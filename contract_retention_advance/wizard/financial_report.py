import base64
from collections import defaultdict
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from .xlsx import workbook


class FinancialReport(models.TransientModel):
    _name = 'contract.financial.report'
    _description = 'As-of contract financial report'
    _rec_name = 'name'
    name = fields.Char(default='Contract Retention & Advance Report', readonly=True)
    report_generated = fields.Boolean(default=False, readonly=True)
    generated_date_from = fields.Date(readonly=True)
    generated_date_to = fields.Date(readonly=True)
    date_from = fields.Date(required=True, default=lambda self: fields.Date.context_today(self).replace(day=1), string='Date From')
    date_to = fields.Date(required=True, default=fields.Date.context_today, string='Date To')
    date = fields.Date(related='date_to', readonly=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, string='Company')
    company_ids = fields.Many2many('res.company', compute='_compute_selected_company')
    partner_id = fields.Many2one('res.partner', string='Customer')
    project_id = fields.Many2one('sale.order', string='Project')
    currency_id = fields.Many2one('res.currency', string='Transaction currency')
    line_ids = fields.One2many('contract.financial.report.line', 'wizard_id', readonly=True)
    file_data = fields.Binary(readonly=True)
    file_name = fields.Char(readonly=True)

    @api.depends('company_id')
    def _compute_selected_company(self):
        for report in self:
            report.company_ids = report.company_id

    @api.constrains('date_from', 'date_to')
    def _check_date_range(self):
        for report in self:
            if report.date_from and report.date_to and report.date_from > report.date_to:
                raise ValidationError(_('Date From must not be later than Date To.'))

    def _domain(self, dated=True):
        self.ensure_one()
        if any(c not in self.env.companies for c in self.company_ids):
            raise AccessError(_('Select only active authorized companies.'))
        domain = [('company_id', 'in', self.company_ids.ids)]
        if dated:
            domain += [('date', '>=', self.date_from), ('date', '<=', self.date_to)]
        if self.partner_id:
            domain += [('partner_id', '=', self.partner_id.commercial_partner_id.id)]
        if self.project_id:
            domain += [('project_id', '=', self.project_id.id)]
        if self.currency_id:
            domain += [('currency_id', '=', self.currency_id.id)]
        return domain

    def _data(self):
        self.ensure_one()
        domain = self._domain()
        movements = self.env['contract.movement'].search(domain + [('move_id.state', '=', 'posted')])
        advances = self.env['contract.advance'].search(domain + [('registration_move_id.state', '=', 'posted')])
        inv_domain = [('state', '=', 'posted'), ('move_type', 'in', ['out_invoice', 'out_refund']),
                      ('company_id', 'in', self.company_ids.ids), ('date', '>=', self.date_from), ('date', '<=', self.date_to)]
        if self.partner_id:
            inv_domain.append(('commercial_partner_id', '=', self.partner_id.commercial_partner_id.id))
        if self.currency_id:
            inv_domain.append(('currency_id', '=', self.currency_id.id))
        invoices = self.env['account.move'].search(inv_domain).filtered(
            lambda m: (m.cra_enabled or m.cra_advance_id or m.cra_movement_ids or m.reversed_entry_id.cra_movement_ids)
            and (not self.project_id or (m.reversed_entry_id.cra_project_id if m.move_type == 'out_refund' else m.cra_project_id) == self.project_id))
        buckets = {}
        def get(company, partner, project, currency):
            key = (company.id, partner.id, project.id, currency.id)
            if key not in buckets:
                buckets[key] = {'company_id': company.id, 'partner_id': partner.id, 'project_id': project.id,
                    'currency_id': currency.id, 'gross': 0, 'held': 0, 'released': 0, 'retention': 0,
                    'received': 0, 'recovered': 0, 'advance': 0, 'payable': 0,
                    'company_retention': 0, 'company_advance': 0,
                    'invoice_ids': self.env['account.move'], 'advance_ids': self.env['contract.advance'],
                    'movement_ids': self.env['contract.movement']}
            return buckets[key]
        for invoice in invoices:
            project = invoice.reversed_entry_id.cra_project_id if invoice.move_type == 'out_refund' else invoice.cra_project_id
            bucket = get(invoice.company_id, invoice.commercial_partner_id, project, invoice.currency_id)
            bucket['gross'] += invoice.amount_total * (-1 if invoice.move_type == 'out_refund' else 1)
            bucket['invoice_ids'] |= invoice
        for advance in advances:
            bucket = get(advance.company_id, advance.partner_id, advance.project_id, advance.currency_id)
            bucket['received'] += advance.amount
            bucket['company_advance'] += sum(advance.registration_move_id.line_ids.mapped('debit'))
            bucket['advance_ids'] |= advance
        for movement in movements:
            bucket = get(movement.company_id, movement.partner_id, movement.project_id, movement.currency_id)
            bucket['movement_ids'] |= movement
            if movement.kind == 'hold':
                bucket['held'] += movement.amount
                bucket['company_retention'] += movement.company_amount
            elif movement.kind in ('hold_reverse', 'hold_reset'):
                bucket['held'] -= movement.amount
                bucket['company_retention'] -= movement.company_amount
            elif movement.kind == 'release':
                bucket['released'] += movement.amount
                bucket['company_retention'] -= movement.company_amount
            elif movement.kind == 'recovery':
                bucket['recovered'] += movement.amount
                bucket['company_advance'] -= movement.company_amount
            elif movement.kind in ('recovery_reverse', 'recovery_reset'):
                bucket['recovered'] -= movement.amount
                bucket['company_advance'] += movement.company_amount
        for bucket in buckets.values():
            currency = self.env['res.currency'].browse(bucket['currency_id'])
            bucket['retention'] = currency.round(bucket['held'] - bucket['released'])
            bucket['advance'] = currency.round(bucket['received'] - bucket['recovered'])
            bucket['payable'] = currency.round(bucket['gross'] - bucket['retention'] - bucket['recovered'])
            for field in ('gross', 'held', 'released', 'received', 'recovered'):
                bucket[field] = currency.round(bucket[field])
        return list(buckets.values()), invoices, advances, movements

    def action_compute(self):
        self.ensure_one()
        self.currency_id = False
        rows, _, _, _ = self._data()
        self.line_ids.unlink()
        values = []
        last_customer = None
        rows.sort(key=lambda row: (self.env['res.partner'].browse(row['partner_id']).display_name or '', row['partner_id'], row['company_id'], row['currency_id'], row['project_id']))
        for row in rows:
            if row['partner_id'] != last_customer:
                values.append({'wizard_id': self.id, 'display_type': 'line_section',
                    'name': self.env['res.partner'].browse(row['partner_id']).display_name or _('Unassigned Customer'),
                    'partner_id': row['partner_id'], 'company_id': row['company_id'], 'currency_id': row['currency_id']})
                last_customer = row['partner_id']
            values.append(dict(row, wizard_id=self.id,
                invoice_ids=[(6, 0, row['invoice_ids'].ids)],
                advance_ids=[(6, 0, row['advance_ids'].ids)],
                movement_ids=[(6, 0, row['movement_ids'].ids)]))
        self.env['contract.financial.report.line'].create(values)
        self.generated_date_from = self.date_from
        self.generated_date_to = self.date_to
        self.report_generated = True
        return False

    def _summary_action(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Contract Retention & Advance Report — %s', self.date),
                'res_model': 'contract.financial.report.line', 'view_mode': 'list',
                'views': [(self.env.ref('contract_retention_advance.financial_report_summary_list').id, 'list')],
                'search_view_id': self.env.ref('contract_retention_advance.financial_report_summary_search').id,
                'domain': [('wizard_id', '=', self.id)],
                'context': dict(self.env.context, contract_report_id=self.id, create=False, edit=False, delete=False), 'target': 'current'}

    @api.model
    def action_open_summary(self):
        report = self.create({})
        return {'type': 'ir.actions.act_window', 'name': _('Contract Retention & Advance Report'),
                'res_model': self._name, 'res_id': report.id, 'view_mode': 'form',
                'views': [(self.env.ref('contract_retention_advance.financial_report_form').id, 'form')],
                'target': 'current'}

    def _print_subtotals(self):
        self._check_generated_report()
        keys = ('gross', 'held', 'released', 'retention', 'received', 'recovered', 'advance', 'payable', 'company_retention', 'company_advance')
        groups = {}
        for line in self.line_ids.filtered(lambda line: not line.display_type):
            key = (line.company_id.id, line.currency_id.id)
            if key not in groups:
                groups[key] = {'company': line.company_id, 'currency': line.currency_id,
                               'company_currency': line.company_currency_id,
                               **{name: 0.0 for name in keys}}
            for name in keys:
                groups[key][name] += getattr(line, name)
        for subtotal in groups.values():
            for name in keys:
                currency = subtotal['company_currency'] if name.startswith('company_') else subtotal['currency']
                subtotal[name] = currency.round(subtotal[name])
        return [groups[key] for key in sorted(groups)]

    def _check_generated_report(self):
        self.ensure_one()
        self.check_access('read')
        if not self.report_generated:
            raise ValidationError(_('Click Get Report before printing or downloading PDF.'))

    def action_pdf(self):
        self._check_generated_report()
        return self.env.ref('contract_retention_advance.financial_report_pdf_action').report_action(self)

    def action_print(self):
        self._check_generated_report()
        return {'type': 'ir.actions.client', 'tag': 'cra_print_financial_report',
                'params': {'url': '/report/html/contract_retention_advance.financial_report_print/%d' % self.id}}

    def action_export(self):
        self.ensure_one()
        rows, invoices, advances, movements = self._data()
        columns = ['Customer', 'Company', 'Project', 'Invoices', 'Currency', 'Gross invoiced',
                   'Net retention held', 'Released', 'Remaining retention', 'Advance received',
                   'Net recovered', 'Remaining advance', 'Payable before cash',
                   'Company currency', 'Retention carrying balance', 'Advance carrying balance']
        heading = ['Contract Retention & Advance Report']
        summary = [heading, [], [], columns]
        totals = defaultdict(lambda: [0.0] * 8)
        precisions = {}
        last_customer = None
        rows.sort(key=lambda row: (self.env['res.partner'].browse(row['partner_id']).display_name or '', row['partner_id'], row['company_id'], row['currency_id'], row['project_id']))
        for row in rows:
            if row['partner_id'] != last_customer:
                summary.append([{'customer_heading': self.env['res.partner'].browse(row['partner_id']).display_name or _('Unassigned Customer')}])
                last_customer = row['partner_id']
            company = self.env['res.company'].browse(row['company_id'])
            currency = self.env['res.currency'].browse(row['currency_id'])
            numeric = [row[k] for k in ['gross', 'held', 'released', 'retention', 'received', 'recovered', 'advance', 'payable']]
            summary.append([self.env['res.partner'].browse(row['partner_id']).display_name,
                company.name, self.env['sale.order'].browse(row['project_id']).name or 'Customer only',
                ', '.join(row['invoice_ids'].mapped('name')), currency.name, *[(v, currency.decimal_places) for v in numeric], 
                company.currency_id.name, (row['company_retention'], company.currency_id.decimal_places),
                (row['company_advance'], company.currency_id.decimal_places)])
            key = (company.name, currency.name)
            precisions[key] = currency.decimal_places
            totals[key] = [a + b for a, b in zip(totals[key], numeric)]
        for (company, currency), values in sorted(totals.items()):
            summary.append(['Subtotal', company, '', '', currency, *[(v, precisions[(company, currency)]) for v in values]])
        source = [['Source transactions'], [], [], ['Type', 'Reference', 'Date', 'Company', 'Customer', 'Project', 'Currency', 'Amount', 'Company amount']]
        for invoice in invoices:
            source.append(['Invoice' if invoice.move_type == 'out_invoice' else 'Credit note', invoice.name,
                str(invoice.date), invoice.company_id.name, invoice.commercial_partner_id.display_name,
                invoice.cra_project_id.name or '', invoice.currency_id.name,
                (invoice.amount_total * (-1 if invoice.move_type == 'out_refund' else 1), invoice.currency_id.decimal_places),
                (invoice.amount_total_signed, invoice.company_id.currency_id.decimal_places)])
        for advance in advances:
            source.append(['Advance', advance.payment_id.name, str(advance.date), advance.company_id.name,
                advance.partner_id.display_name, advance.project_id.name or '', advance.currency_id.name,
                (advance.amount, advance.currency_id.decimal_places),
                (sum(advance.registration_move_id.line_ids.mapped('debit')), advance.company_id.currency_id.decimal_places)])
        for m in movements:
            source.append([m.kind, m.move_id.name, str(m.date), m.company_id.name, m.partner_id.display_name,
                m.project_id.name or '', m.currency_id.name, (m.amount, m.currency_id.decimal_places), (m.company_amount, m.company_id.currency_id.decimal_places)])
        notes = [['Method'], ['Posted transactions within the inclusive date range only; dates are accounting dates, not creation dates.'],
                 ['Opening balances before Date From are excluded. Remaining amounts are net period amounts, not closing account balances.'],
                 ['Customer/project/company/transaction-currency buckets; no cross-currency grand total.'],
                 ['Net held and recovered include proportional credit corrections.'],
                 ['Payable before cash is cumulative normal AR origin, not current unpaid residual.'],
                 ['Company carrying amounts use posted module transfers, not current-rate revaluation.'],
                 ['External manual reconciliation/revaluation must be independently checked against general ledger.']]
        data = workbook([('Summary', summary), ('Source transactions', source), ('Method', notes)])
        self.write({'file_data': base64.b64encode(data), 'file_name': 'contract-report-%s-to-%s.xlsx' % (self.date_from, self.date_to)})
        return {'type': 'ir.actions.act_url', 'url': '/web/content/contract.financial.report/%d/file_data/%s?download=true' % (self.id, self.file_name), 'target': 'self'}


class ReportLine(models.TransientModel):
    _name = 'contract.financial.report.line'
    _description = 'Contract report bucket'
    display_type = fields.Selection([('line_section', 'Customer Heading')], default=False)
    name = fields.Char(readonly=True)
    wizard_id = fields.Many2one('contract.financial.report', required=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True)
    partner_id = fields.Many2one('res.partner')
    project_id = fields.Many2one('sale.order')
    currency_id = fields.Many2one('res.currency', required=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id', string='Company currency')
    gross = fields.Monetary()
    held = fields.Monetary()
    released = fields.Monetary()
    retention = fields.Monetary()
    received = fields.Monetary()
    recovered = fields.Monetary()
    advance = fields.Monetary()
    payable = fields.Monetary()
    company_retention = fields.Monetary(currency_field='company_currency_id')
    company_advance = fields.Monetary(currency_field='company_currency_id')
    invoice_ids = fields.Many2many('account.move')
    advance_ids = fields.Many2many('contract.advance', string='Advance records')
    movement_ids = fields.Many2many('contract.movement')

    def action_invoices(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.invoice_ids.ids)]}

    def action_movements(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'contract.movement', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.movement_ids.ids)]}

    def action_advances(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'contract.advance', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.advance_ids.ids)]}


    def _report_from_screen(self):
        report = self.env['contract.financial.report'].browse(self.env.context.get('contract_report_id')).exists()
        if not report or len(report) != 1:
            raise AccessError(_('Reopen the report menu to load a fresh report.'))
        report.check_access('read')
        return report

    def action_report_filters(self):
        report = self._report_from_screen()
        return {'type': 'ir.actions.act_window', 'name': _('Report Filters'), 'res_model': report._name, 'res_id': report.id, 'view_mode': 'form', 'target': 'current'}

    def action_report_refresh(self):
        return self._report_from_screen().action_compute()

    def action_report_export(self):
        return self._report_from_screen().action_export()
