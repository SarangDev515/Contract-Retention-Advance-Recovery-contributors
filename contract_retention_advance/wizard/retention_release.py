from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError
from ..models.common import INTERNAL, require_accountant, lock, transfer, reconcile


class ReleaseWizard(models.TransientModel):
    _name = 'contract.retention.release.wizard'
    _description = 'Release selected retention'
    _check_company_auto = True
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    partner_id = fields.Many2one('res.partner', string='Customer')
    project_id = fields.Many2one('sale.order', check_company=True, string='Project')
    currency_id = fields.Many2one('res.currency', required=True, default=lambda s: s.env.company.currency_id)
    retention_ids = fields.Many2many('contract.retention', string='Eligible retention items', required=True)
    invoice_ids = fields.Many2many('account.move', compute='_compute_available', string='Source invoices')
    date = fields.Date(required=True, default=fields.Date.context_today)
    journal_id = fields.Many2one('account.journal', required=True, check_company=True, default=lambda s: s.env.company.cra_journal_id)
    description = fields.Char(default='Retention release', required=True)
    available = fields.Monetary(compute='_compute_available')
    amount = fields.Monetary(required=True)
    processed_move_id = fields.Many2one('account.move', readonly=True, copy=False)

    @api.model
    def default_get(self, field_names):
        values = super().default_get(field_names)
        commands = values.get('retention_ids', [])
        ids = next((command[2] for command in commands if command[0] == 6), [])
        records = self.env['contract.retention'].browse(ids).exists()
        if records:
            first = records[0]
            defaults = {'company_id': first.company_id.id, 'partner_id': first.partner_id.id,
                        'project_id': first.project_id.id, 'currency_id': first.currency_id.id,
                        'journal_id': first.company_id.cra_journal_id.id,
                        'amount': sum(records.mapped('remaining'))}
            values.update({k: v for k, v in defaults.items() if k in field_names})
        return values

    @api.depends('retention_ids', 'retention_ids.remaining')
    def _compute_available(self):
        for wizard in self:
            wizard.available = sum(wizard.retention_ids.mapped('remaining'))
            wizard.invoice_ids = wizard.retention_ids.invoice_id

    @api.onchange('retention_ids')
    def _onchange_retention(self):
        if self.retention_ids:
            first = self.retention_ids[0]
            self.company_id = first.company_id
            self.partner_id = first.partner_id
            self.currency_id = first.currency_id
            self.project_id = first.project_id
            self.journal_id = first.company_id.cra_journal_id
            self.amount = self.available

    @api.model_create_multi
    def create(self, vals_list):
        if any(v.get('processed_move_id') for v in vals_list):
            raise AccessError(_('Release results cannot be set manually.'))
        return super().create(vals_list)

    def write(self, vals):
        if any(self.mapped('processed_move_id')) or ('processed_move_id' in vals and self.env.context.get('cra_internal') is not INTERNAL):
            raise AccessError(_('A completed release request is immutable.'))
        return super().write(vals)

    def action_release(self):
        self.ensure_one()
        require_accountant(self.env)
        lock(self)
        if self.processed_move_id:
            return self._move_action()
        records = self.retention_ids.sorted('id')
        lock(records.invoice_id)
        lock(records)
        if not records:
            raise UserError(_('Select retention items.'))
        self.company_id._cra_check_config()
        for record in records:
            if (record.company_id != self.company_id or record.currency_id != self.currency_id or
                    record.partner_id != self.partner_id or record.project_id != self.project_id):
                raise UserError(_('Select items from one company, customer, project and currency.'))
            if self.date < record.invoice_id.date:
                raise UserError(_('Release cannot predate its source invoice.'))
        available = self.currency_id.round(sum(records.mapped('remaining')))
        if self.amount <= 0 or self.currency_id.compare_amounts(self.amount, available) > 0:
            raise UserError(_('Release amount must be positive and cannot exceed selected remaining retention.'))
        remaining = self.currency_id.round(self.amount)
        entries = self.env['account.move']
        for record in records:
            amount = min(remaining, record.remaining)
            if self.currency_id.is_zero(amount):
                continue
            holds = record.movement_ids.filtered(lambda m: m.kind == 'hold')
            account = holds.move_id.line_ids.filtered(lambda l: l.debit > 0).account_id
            open_lines = record.movement_ids.move_id.line_ids.filtered(lambda l: l.account_id == account and not l.reconciled)
            current_currency = sum(open_lines.mapped('amount_residual_currency'))
            current_balance = sum(open_lines.mapped('amount_residual'))
            if self.currency_id.compare_amounts(current_currency, amount) < 0:
                raise UserError(_('Retention ledger was altered externally. Reconcile the discrepancy first.'))
            carrying_balance = self.company_id.currency_id.round(current_balance * amount / current_currency)
            ar = record.invoice_id._cra_receivable().account_id
            entry = transfer(self.env, self.company_id, self.journal_id, self.currency_id,
                self.date, self.partner_id, ar, account, amount, self.description,
                debit_balance=carrying_balance, credit_balance=carrying_balance)
            record.invoice_id._cra_record('release', entry, amount, retention=record)
            reconcile(open_lines | entry.line_ids.filtered(lambda l: l.account_id == account))
            entries |= entry
            remaining = self.currency_id.round(remaining - amount)
        self.with_context(cra_internal=INTERNAL).write({'processed_move_id': entries[0].id})
        return {'type': 'ir.actions.act_window', 'name': _('Posted retention releases'),
                'res_model': 'account.move', 'view_mode': 'list,form', 'domain': [('id', 'in', entries.ids)]}

    def _move_action(self):
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'view_mode': 'form',
                'res_id': self.processed_move_id.id}
