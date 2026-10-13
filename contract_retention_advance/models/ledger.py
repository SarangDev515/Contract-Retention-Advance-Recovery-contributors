from odoo import api, fields, models, _, Command
from odoo.exceptions import UserError, AccessError, ValidationError
from .common import INTERNAL, require_accountant, lock, transfer, reconcile


class Immutable(models.AbstractModel):
    _name = 'contract.immutable'
    _description = 'Protected accounting audit records'

    @api.model_create_multi
    def create(self, values):
        if self.env.context.get('cra_internal') is not INTERNAL:
            raise AccessError(_('Use the authorized contract accounting actions to create audit records.'))
        return super().create(values)

    def write(self, values):
        raise AccessError(_('Accounting audit records are immutable. Create a correction instead.'))

    def unlink(self):
        raise AccessError(_('Accounting audit records cannot be deleted.'))


class Advance(models.Model):
    _name = 'contract.advance'
    _description = 'Deliberately assigned customer advance'
    _check_company_auto = True
    _rec_name = 'payment_id'
    payment_id = fields.Many2one('account.payment', required=True, check_company=True, ondelete='restrict')
    payment_move_id = fields.Many2one(related='payment_id.move_id', store=True)
    company_id = fields.Many2one(related='payment_id.company_id', store=True)
    partner_id = fields.Many2one(related='payment_id.partner_id.commercial_partner_id', store=True)
    currency_id = fields.Many2one(related='payment_id.currency_id', store=True)
    project_id = fields.Many2one('sale.order', check_company=True, ondelete='restrict')
    registration_move_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True, ondelete='restrict')
    date = fields.Date(related='registration_move_id.date', store=True)
    amount = fields.Monetary(readonly=True, copy=False)
    recovered = fields.Monetary(compute='_compute_balance')
    remaining = fields.Monetary(compute='_compute_balance')
    movement_ids = fields.One2many('contract.movement', 'advance_id', readonly=True)
    _unique_payment = models.Constraint('UNIQUE(payment_id)', 'This payment is already assigned as an advance.')

    @api.depends('amount', 'currency_id', 'registration_move_id.state', 'movement_ids.amount', 'movement_ids.kind', 'movement_ids.move_id.state')
    def _compute_balance(self):
        for advance in self:
            if not advance.currency_id:
                advance.recovered = 0.0
                advance.remaining = 0.0
                continue
            posted = advance.movement_ids.filtered(lambda m: m.move_id.state == 'posted')
            advance.recovered = advance.currency_id.round(sum(posted.filtered(lambda m: m.kind == 'recovery').mapped('amount'))
                - sum(posted.filtered(lambda m: m.kind in ('recovery_reverse', 'recovery_reset')).mapped('amount')))
            advance.remaining = advance.currency_id.round((advance.amount if advance.registration_move_id.state == 'posted' else 0) - advance.recovered)

    @api.constrains('payment_id', 'project_id')
    def _check_project(self):
        for advance in self:
            if advance.project_id and (advance.project_id.company_id != advance.company_id or
                advance.project_id.partner_id.commercial_partner_id != advance.partner_id):
                raise ValidationError(_('The project must belong to the advance customer and company.'))

    @api.model_create_multi
    def create(self, values):
        require_accountant(self.env)
        for vals in values:
            if any(k in vals for k in ('registration_move_id', 'amount')):
                raise AccessError(_('Advance amounts and entries are created by registration only.'))
        return super().create(values)

    def write(self, vals):
        if self.env.context.get('cra_internal') is not INTERNAL:
            require_accountant(self.env)
            if any(a.registration_move_id for a in self) or any(k in vals for k in ('registration_move_id', 'amount')):
                raise AccessError(_('Registered advances are immutable.'))
        return super().write(vals)

    def unlink(self):
        if any(a.registration_move_id for a in self):
            raise AccessError(_('Registered advances cannot be deleted.'))
        return super().unlink()

    def action_register(self):
        require_accountant(self.env)
        lock(self)
        for advance in self:
            if advance.registration_move_id:
                raise UserError(_('This advance is already registered.'))
            payment = advance.payment_id
            lock(payment.move_id)
            company = advance.company_id
            company._cra_check_config()
            advance._check_project()
            if payment.payment_type != 'inbound' or payment.partner_type != 'customer' or payment.move_id.state != 'posted':
                raise UserError(_('Choose an inbound customer payment with a posted journal entry. Configure an outstanding receipts account if the payment has no entry.'))
            lines = payment.move_id.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
            if len(lines) != 1 or lines.reconciled or lines.matched_debit_ids or lines.matched_credit_ids:
                raise UserError(_('The advance must be an entirely unreconciled customer payment.'))
            line = lines.ensure_one()
            if line.currency_id != advance.currency_id or line.amount_residual_currency >= 0:
                raise UserError(_('The payment receivable credit must use the payment currency.'))
            amount = advance.currency_id.round(-line.amount_residual_currency)
            move = transfer(self.env, company, company.cra_journal_id, advance.currency_id,
                payment.date, advance.partner_id, line.account_id, company.cra_advance_account_id,
                amount, _('Advance registration: %s', payment.name),
                debit_balance=-line.amount_residual, credit_balance=-line.amount_residual)
            reconcile(line | move.line_ids.filtered(lambda l: l.account_id == line.account_id))
            advance.with_context(cra_internal=INTERNAL).write({'registration_move_id': move.id, 'amount': amount})
        return True

    def action_movements(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Advance allocations'),
                'res_model': 'contract.movement', 'view_mode': 'list,form',
                'domain': [('advance_id', '=', self.id)]}


class Retention(models.Model):
    _name = 'contract.retention'
    _inherit = 'contract.immutable'
    _description = 'Invoice retention balance'
    _check_company_auto = True
    _rec_name = 'invoice_id'
    invoice_id = fields.Many2one('account.move', required=True, check_company=True, ondelete='restrict')
    company_id = fields.Many2one(related='invoice_id.company_id', store=True)
    partner_id = fields.Many2one(related='invoice_id.commercial_partner_id', store=True)
    currency_id = fields.Many2one(related='invoice_id.currency_id', store=True)
    project_id = fields.Many2one('sale.order', check_company=True, ondelete='restrict')
    handover_date = fields.Date()
    movement_ids = fields.One2many('contract.movement', 'retention_id', readonly=True)
    held = fields.Monetary(compute='_compute_balance')
    released = fields.Monetary(compute='_compute_balance')
    reversed_amount = fields.Monetary(compute='_compute_balance')
    remaining = fields.Monetary(compute='_compute_balance')
    _unique_invoice = models.Constraint('UNIQUE(invoice_id)', 'An invoice can only have one retention record.')

    @api.depends('currency_id', 'movement_ids.amount', 'movement_ids.kind', 'movement_ids.move_id.state')
    def _compute_balance(self):
        for rec in self:
            if not rec.currency_id:
                rec.held = 0.0
                rec.released = 0.0
                rec.reversed_amount = 0.0
                rec.remaining = 0.0
                continue
            posted = rec.movement_ids.filtered(lambda m: m.move_id.state == 'posted')
            rec.held = rec.currency_id.round(sum(posted.filtered(lambda m: m.kind == 'hold').mapped('amount')))
            rec.released = rec.currency_id.round(sum(posted.filtered(lambda m: m.kind == 'release').mapped('amount')))
            rec.reversed_amount = rec.currency_id.round(sum(posted.filtered(lambda m: m.kind in ('hold_reverse', 'hold_reset')).mapped('amount')))
            rec.remaining = rec.currency_id.round(rec.held - rec.released - rec.reversed_amount)

    def action_release(self):
        return {'type': 'ir.actions.act_window', 'name': _('Release Retention'),
                'res_model': 'contract.retention.release.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_retention_ids': [Command.set(self.ids)]}}

    def action_movements(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Retention movements'),
                'res_model': 'contract.movement', 'view_mode': 'list,form',
                'domain': [('retention_id', '=', self.id)]}


class Movement(models.Model):
    _name = 'contract.movement'
    _inherit = 'contract.immutable'
    _description = 'Posted contract accounting movement'
    _check_company_auto = True
    _order = 'date, id'
    kind = fields.Selection([('hold', 'Retention held'), ('release', 'Retention release'),
                            ('hold_reverse', 'Retention credit reversal'), ('recovery', 'Advance recovery'),
                            ('recovery_reverse', 'Advance credit reversal'),
                            ('hold_reset', 'Retention reset compensation'),
                            ('recovery_reset', 'Advance reset compensation')], required=True)
    cycle = fields.Integer(default=0, readonly=True)
    move_id = fields.Many2one('account.move', required=True, check_company=True, ondelete='restrict')
    invoice_id = fields.Many2one('account.move', required=True, check_company=True, ondelete='restrict')
    retention_id = fields.Many2one('contract.retention', check_company=True, ondelete='restrict')
    advance_id = fields.Many2one('contract.advance', check_company=True, ondelete='restrict')
    original_id = fields.Many2one('contract.movement', check_company=True, ondelete='restrict')
    company_id = fields.Many2one(related='move_id.company_id', store=True)
    currency_id = fields.Many2one(related='invoice_id.currency_id', store=True)
    partner_id = fields.Many2one(related='invoice_id.commercial_partner_id', store=True)
    project_id = fields.Many2one('sale.order', check_company=True, ondelete='restrict')
    date = fields.Date(related='move_id.date', store=True)
    amount = fields.Monetary(required=True)
    company_amount = fields.Monetary(currency_field='company_currency_id', required=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id', string='Company currency')
    _unique_invoice_kind = models.Constraint('UNIQUE(invoice_id, kind, original_id)', 'Duplicate credit reversal allocation.')

    @api.constrains('amount', 'move_id', 'retention_id', 'advance_id')
    def _check_values(self):
        for rec in self:
            if rec.amount <= 0 or rec.move_id.state != 'posted' or not rec.move_id.cra_protected:
                raise ValidationError(_('Movements require a positive amount and a posted protected entry.'))
            if bool(rec.retention_id) == bool(rec.advance_id):
                raise ValidationError(_('A movement must identify exactly one retention or advance.'))
