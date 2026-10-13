from odoo import api, fields, models, _, Command
from odoo.exceptions import UserError
from ..models.common import require_accountant, lock


class Advance(models.Model):
    _inherit = 'contract.advance'


    receipt_move_id = fields.Many2one('account.move', compute='_compute_bank_settlement', string='Bank Receipt', readonly=True)
    bank_reconciled = fields.Boolean(compute='_compute_bank_settlement')
    bank_settlement_state = fields.Selection([('pending', 'Pending'), ('reconciled', 'Reconciled')], compute='_compute_bank_settlement', string='Bank Settlement')

    @api.depends('payment_id.state', 'payment_id.move_id.line_ids.amount_residual', 'payment_id.move_id.line_ids.reconciled')
    def _compute_bank_settlement(self):
        for advance in self:
            payment = advance.payment_id
            receipt = self.env['account.move'].search([('cra_receipt_advance_id', '=', advance.id)], limit=1) if advance.id else self.env['account.move']
            lines = payment.move_id.line_ids.filtered(lambda line: line.account_id == payment.outstanding_account_id)
            settled = bool(payment.move_id.state == 'posted' and lines and all(lines.mapped('reconciled')) and payment.state == 'paid')
            advance.receipt_move_id = receipt
            advance.bank_reconciled = settled
            advance.bank_settlement_state = 'reconciled' if settled else 'pending'

    def action_create_bank_receipt(self):
        self.ensure_one()
        require_accountant(self.env)
        self.check_access('read')
        receipt = self.env['account.move'].search([('cra_receipt_advance_id', '=', self.id)], limit=1)
        if receipt:
            return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'res_id': receipt.id, 'view_mode': 'form', 'target': 'current'}
        wizard = self.env['contract.bank.receipt.wizard'].create({'advance_id': self.id})
        return {'type': 'ir.actions.act_window', 'name': _('Create Bank Receipt'), 'res_model': wizard._name, 'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def action_reconcile_payment(self):
        self.ensure_one()
        require_accountant(self.env)
        self.check_access('read')
        wizard = self.env['contract.payment.reconcile.wizard'].create({'advance_id': self.id})
        wizard._source_line()
        return {'type': 'ir.actions.act_window', 'name': _('Reconcile Payment'),
                'res_model': wizard._name, 'view_mode': 'form', 'res_id': wizard.id,
                'target': 'new'}


class PaymentReconcile(models.TransientModel):
    _name = 'contract.payment.reconcile.wizard'
    _description = 'Match an existing bank receipt to a contract advance payment'
    _check_company_auto = True

    advance_id = fields.Many2one('contract.advance', required=True, readonly=True)
    payment_id = fields.Many2one(related='advance_id.payment_id', readonly=True)
    company_id = fields.Many2one(related='payment_id.company_id', readonly=True)
    currency_id = fields.Many2one(related='payment_id.currency_id', readonly=True)
    account_id = fields.Many2one('account.account', compute='_compute_candidates')
    outstanding_amount = fields.Monetary(compute='_compute_candidates')
    candidate_ids = fields.Many2many('account.move.line', compute='_compute_candidates')
    receipt_line_id = fields.Many2one('account.move.line', string='Existing bank receipt credit', check_company=True)
    receipt_move_id = fields.Many2one(related='receipt_line_id.move_id', readonly=True)
    receipt_reference = fields.Char(related='receipt_line_id.move_id.ref', readonly=True)
    no_candidates = fields.Boolean(compute='_compute_candidates')

    def _source_line(self):
        self.ensure_one()
        require_accountant(self.env)
        advance = self.advance_id.exists()
        if not advance:
            raise UserError(_('The advance no longer exists.'))
        advance.check_access('read')
        payment = advance.payment_id.exists()
        payment.check_access('write')
        if payment.company_id not in self.env.companies:
            raise UserError(_('Activate the payment company first.'))
        if not advance.registration_move_id or advance.registration_move_id.state != 'posted':
            raise UserError(_('The advance registration must be posted.'))
        if payment.payment_type != 'inbound' or payment.partner_type != 'customer' or payment.state != 'in_process' or payment.move_id.state != 'posted':
            raise UserError(_('Choose an In Process customer payment with a posted journal entry.'))
        # Version one intentionally excludes FX settlement and exchange-difference creation.
        if payment.currency_id != payment.company_id.currency_id:
            raise UserError(_('This wizard supports company-currency payments only. Use the accounting reconciliation tool for foreign currency.'))
        account = payment.outstanding_account_id
        if not account or not account.reconcile or account == payment.journal_id.default_account_id:
            raise UserError(_('A separate reconcilable Outstanding Receipts account is required.'))
        lines = payment.move_id.line_ids.filtered(lambda line: line.account_id == account)
        if len(lines) != 1:
            raise UserError(_('The payment must have exactly one Outstanding Receipts line.'))
        line = lines.ensure_one()
        line.check_access('write')
        if line.reconciled or line.matched_debit_ids or line.matched_credit_ids or line.amount_residual <= 0:
            raise UserError(_('The Outstanding Receipts debit must be wholly unmatched and positive.'))
        return line

    def _validate_receipt(self, source, receipt):
        if not receipt.exists():
            raise UserError(_('The selected receipt no longer exists.'))
        receipt.check_access('write')
        payment = self.payment_id
        currency = payment.company_id.currency_id
        if receipt.move_id.state != 'posted' or receipt.move_id == source.move_id or receipt.move_id.cra_protected:
            raise UserError(_('Select a separate posted bank receipt, not a contract transfer or cancelled entry.'))
        if receipt.company_id != source.company_id or receipt.account_id != source.account_id or receipt.currency_id != source.currency_id:
            raise UserError(_('The receipt company, account and currency must match the payment.'))
        if receipt.partner_id.commercial_partner_id != payment.partner_id.commercial_partner_id:
            raise UserError(_('The receipt must belong to the same customer.'))
        if receipt.reconciled or receipt.matched_debit_ids or receipt.matched_credit_ids or receipt.amount_residual >= 0:
            raise UserError(_('Select a wholly unmatched Outstanding Receipts credit.'))
        if not currency.is_zero(source.amount_residual + receipt.amount_residual):
            raise UserError(_('Only equal full residual amounts may be matched. No partial payments or write-offs are supported.'))
        if not currency.is_zero(source.amount_residual_currency + receipt.amount_residual_currency):
            raise UserError(_('The currency residuals do not match.'))
        bank = payment.journal_id.default_account_id
        other = receipt.move_id.line_ids.filtered(lambda line: line.display_type not in ('line_section', 'line_subsection', 'line_note') and line != receipt)
        if len(other) != 1 or other.account_id != bank or other.currency_id != source.currency_id or other.partner_id.commercial_partner_id != payment.partner_id.commercial_partner_id or not currency.is_zero(other.balance + receipt.balance) or other.balance <= 0:
            raise UserError(_('The receipt must be a two-line transfer debiting this payment journal bank account and crediting Outstanding Receipts for this customer.'))

    @api.depends('advance_id', 'payment_id.move_id.line_ids.amount_residual')
    def _compute_candidates(self):
        for wizard in self:
            wizard.account_id = False
            wizard.outstanding_amount = 0
            wizard.candidate_ids = False
            wizard.no_candidates = True
            if not wizard.advance_id:
                continue
            source = wizard._source_line()
            wizard.account_id = source.account_id
            wizard.outstanding_amount = source.amount_residual
            candidates = self.env['account.move.line'].search([
                ('company_id', '=', source.company_id.id), ('account_id', '=', source.account_id.id),
                ('currency_id', '=', source.currency_id.id), ('parent_state', '=', 'posted'),
                ('reconciled', '=', False), ('amount_residual', '<', 0),
                ('partner_id.commercial_partner_id', '=', wizard.payment_id.partner_id.commercial_partner_id.id),
            ])
            valid = self.env['account.move.line']
            for receipt in candidates:
                try:
                    wizard._validate_receipt(source, receipt)
                except UserError:
                    continue
                valid |= receipt
            wizard.candidate_ids = valid
            wizard.no_candidates = not valid

    def action_reconcile(self):
        self.ensure_one()
        require_accountant(self.env)
        self.check_access('write')
        source = self._source_line()
        receipt = self.receipt_line_id.exists()
        if not receipt:
            raise UserError(_('Select an existing posted bank receipt. This wizard never creates a receipt.'))
        # Lock moves first (ordered), then lines; refresh and validate after locks.
        lock(source.move_id | receipt.move_id)
        lock(source | receipt)
        source = self._source_line()
        self._validate_receipt(source, receipt)
        payment = self.payment_id
        amount = source.amount_residual
        source_move = source.move_id.name
        receipt_move = receipt.move_id.name
        (source | receipt).reconcile()
        if not source.reconciled or not receipt.reconciled:
            raise UserError(_('The full match was not completed. The transaction has been rolled back.'))
        # Let Odoo compute its stored state; never write Paid directly.
        state = payment.state
        payment.message_post(body=_('Matched %(amount)s %(currency)s Outstanding Receipts: %(payment)s against %(receipt)s. Payment status: %(state)s.',
                                    amount=amount, currency=payment.currency_id.name,
                                    payment=source_move, receipt=receipt_move, state=state))
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Outstanding Receipts matched'),
                           'message': _('Payment status: %s', dict(payment._fields['state']._description_selection(self.env)).get(state, state)),
                           'type': 'success', 'sticky': True,
                           'next': {'type': 'ir.actions.client', 'tag': 'reload'}}}


class BankReceiptMove(models.Model):
    _inherit = 'account.move'

    cra_receipt_advance_id = fields.Many2one('contract.advance', string='Contract Advance Bank Receipt', check_company=True, copy=False, ondelete='restrict', readonly=True)
    _cra_unique_bank_receipt = models.Constraint('UNIQUE(cra_receipt_advance_id)', 'A bank receipt is already linked to this advance. Open the existing receipt instead.')


    def write(self, vals):
        if 'cra_receipt_advance_id' in vals and any(move.cra_receipt_advance_id.id != (vals['cra_receipt_advance_id'] or False) for move in self):
            raise UserError(_('The bank receipt advance link cannot be changed.'))
        return super().write(vals)


class BankReceiptWizard(models.TransientModel):
    _name = 'contract.bank.receipt.wizard'
    _description = 'Prepare a bank receipt draft for a contract advance'
    _check_company_auto = True

    advance_id = fields.Many2one('contract.advance', required=True, readonly=True)
    company_id = fields.Many2one(related='advance_id.company_id', readonly=True)
    currency_id = fields.Many2one(related='advance_id.currency_id', readonly=True)
    payment_id = fields.Many2one(related='advance_id.payment_id', readonly=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    journal_id = fields.Many2one('account.journal', check_company=True, domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")
    reference = fields.Char()
    received_confirmed = fields.Boolean(string='I confirm the bank received this money and no receipt was recorded elsewhere')
    amount = fields.Monetary(compute='_compute_receipt_amount')

    @api.depends('advance_id')
    def _compute_receipt_amount(self):
        for wizard in self:
            wizard.amount = wizard.payment_id.amount

    def action_create_draft(self):
        self.ensure_one()
        require_accountant(self.env)
        self.check_access('write')
        advance = self.advance_id
        # Serialize creation against the advance and payment; no elevated permissions.
        lock(advance)
        lock(advance.payment_id)
        existing = self.env['account.move'].search([('cra_receipt_advance_id', '=', advance.id)], limit=1)
        if existing:
            raise UserError(_('A receipt already exists for this advance. Open the existing Bank Receipt.'))
        if not self.journal_id or not self.reference or not self.reference.strip():
            raise UserError(_('Select a Journal and enter a bank receipt reference before creating the draft.'))
        if not self.received_confirmed:
            raise UserError(_('Confirm the actual bank receipt and check that it was not already recorded.'))
        if self.journal_id.type != 'general' or self.journal_id.company_id != advance.company_id or advance.company_id not in self.env.companies:
            raise UserError(_('Choose a miscellaneous journal in the active payment company.'))
        reconciliation = self.env['contract.payment.reconcile.wizard'].create({'advance_id': advance.id})
        source = reconciliation._source_line()
        bank = advance.payment_id.journal_id.default_account_id
        if not bank or bank == source.account_id:
            raise UserError(_('Configure a separate actual bank account on the payment journal first.'))
        # Conservative duplicate check across draft and posted receipts, irrespective of reference.
        possible = self.env['account.move.line'].search([
            ('company_id', '=', advance.company_id.id), ('account_id', '=', source.account_id.id),
            ('parent_state', 'in', ['draft', 'posted']),
            ('partner_id.commercial_partner_id', '=', advance.partner_id.id), ('credit', '>', 0),
        ])
        for line in possible:
            if advance.currency_id.is_zero(line.credit - source.amount_residual) and any(l.account_id == bank and l.debit > 0 for l in line.move_id.line_ids):
                raise UserError(_('A possible bank receipt already exists: %s. Review it before creating another.', line.move_id.display_name))
        move = self.env['account.move'].create({
            'move_type': 'entry', 'company_id': advance.company_id.id, 'journal_id': self.journal_id.id,
            'date': self.date, 'ref': self.reference, 'cra_receipt_advance_id': advance.id,
            'line_ids': [Command.create({'account_id': bank.id, 'partner_id': advance.partner_id.id, 'name': self.reference, 'debit': source.amount_residual, 'credit': 0, 'currency_id': advance.currency_id.id, 'amount_currency': source.amount_residual}),
                         Command.create({'account_id': source.account_id.id, 'partner_id': advance.partner_id.id, 'name': self.reference, 'debit': 0, 'credit': source.amount_residual, 'currency_id': advance.currency_id.id, 'amount_currency': -source.amount_residual})],
        })
        advance.payment_id.message_post(body=_('Draft bank receipt %s created from the contract advance. Review and post separately before reconciliation.', move.display_name))
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'res_id': move.id, 'view_mode': 'form', 'target': 'current'}
