from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError, AccessError
from .common import INTERNAL, require_accountant, lock, transfer, reconcile


class AccountMove(models.Model):
    _inherit = 'account.move'
    cra_protected = fields.Boolean(copy=False, readonly=True)
    cra_cycle = fields.Integer(default=0, copy=False, readonly=True)
    cra_source_advance_ids = fields.One2many('contract.advance', 'payment_move_id', readonly=True)
    cra_enabled = fields.Boolean(string='Enable retention', copy=False)
    cra_retention_percent = fields.Float(string='Retention %', default=lambda s: s.env.company.cra_retention_percent)
    cra_handover_date = fields.Date(string='Expected handover date')
    cra_advance_id = fields.Many2one('contract.advance', string='Assigned advance', check_company=True, copy=False)
    cra_recovery_method = fields.Selection([('percent', 'Percentage'), ('fixed', 'Fixed amount')], default='percent', required=True)
    cra_recovery_percent = fields.Float(string='Recovery %', default=lambda s: s.env.company.cra_recovery_percent)
    cra_fixed_recovery = fields.Monetary(string='Fixed recovery')
    cra_project_id = fields.Many2one('sale.order', string='Sales order / project', compute='_compute_cra_project', store=True)
    cra_base = fields.Monetary(string='Eligible work', compute='_compute_cra_amounts')
    cra_retention = fields.Monetary(string='Retention deduction', compute='_compute_cra_amounts')
    cra_recovery = fields.Monetary(string='Advance deduction', compute='_compute_cra_amounts')
    cra_payable = fields.Monetary(string='Immediate payable (before cash payments)', compute='_compute_cra_amounts')
    cra_advance_original = fields.Monetary(related='cra_advance_id.amount', string='Original advance')
    cra_advance_recovered = fields.Monetary(related='cra_advance_id.recovered', string='Advance recovered to date')
    cra_advance_remaining = fields.Monetary(related='cra_advance_id.remaining', string='Available advance')
    cra_advance_after = fields.Monetary(compute='_compute_cra_amounts', string='Advance after this invoice')
    cra_retention_ids = fields.One2many('contract.retention', 'invoice_id', readonly=True)
    cra_movement_ids = fields.One2many('contract.movement', 'invoice_id', readonly=True)
    cra_released = fields.Monetary(compute='_compute_cra_amounts', string='Retention released')
    cra_remaining_retention = fields.Monetary(compute='_compute_cra_amounts', string='Remaining retention')

    @api.depends('invoice_line_ids.sale_line_ids.order_id')
    def _compute_cra_project(self):
        for move in self:
            orders = move.invoice_line_ids.sale_line_ids.order_id
            move.cra_project_id = orders if len(orders) == 1 else False

    @api.depends('invoice_line_ids.price_subtotal', 'amount_total', 'cra_enabled', 'cra_retention_percent',
                 'cra_advance_id', 'cra_recovery_method', 'cra_recovery_percent', 'cra_fixed_recovery',
                 'cra_advance_id.remaining', 'cra_retention_ids.remaining', 'cra_retention_ids.released',
                 'cra_movement_ids.amount', 'cra_movement_ids.kind', 'cra_movement_ids.cycle', 'cra_cycle', 'cra_movement_ids.move_id.state', 'state', 'reversed_entry_id')
    def _compute_cra_amounts(self):
        for move in self:
            currency = move.currency_id
            base = currency.round(sum(move.invoice_line_ids.filtered(lambda l: l.display_type == 'product').mapped('price_subtotal')))
            retention = currency.round(base * move.cra_retention_percent / 100) if move.cra_enabled else 0
            recovery = (currency.round(base * move.cra_recovery_percent / 100) if move.cra_recovery_method == 'percent'
                        else currency.round(move.cra_fixed_recovery)) if move.cra_advance_id else 0
            if move.state == 'posted' and (move.cra_movement_ids or (move.move_type == 'out_refund' and move.reversed_entry_id.cra_movement_ids)):
                posted = move.cra_movement_ids.filtered(lambda m: m.move_id.state == 'posted' and m.cycle == move.cra_cycle)
                retention = sum(posted.filtered(lambda m: m.kind in ('hold', 'hold_reverse')).mapped('amount'))
                recovery = sum(posted.filtered(lambda m: m.kind in ('recovery', 'recovery_reverse')).mapped('amount'))
            elif move.move_type == 'out_refund' and move.reversed_entry_id.cra_movement_ids:
                original = move.reversed_entry_id
                ratio = max(base, 0) / original.cra_base if original.cra_base > 0 else 0
                retention = min(currency.round(original.cra_retention * ratio), sum(original.cra_retention_ids.mapped('remaining'))) 
                recovery = currency.round(original.cra_recovery * ratio)
            move.cra_base = base
            move.cra_retention = currency.round(retention)
            move.cra_recovery = currency.round(recovery)
            move.cra_payable = currency.round(move.amount_total - retention - recovery)
            move.cra_advance_after = currency.round(move.cra_advance_id.remaining - (recovery if move.state != 'posted' else 0))
            move.cra_released = sum(move.cra_retention_ids.mapped('released'))
            move.cra_remaining_retention = sum(move.cra_retention_ids.mapped('remaining'))

    @api.constrains('cra_retention_percent', 'cra_recovery_percent', 'cra_fixed_recovery')
    def _check_cra_percent(self):
        for move in self:
            if not 0 <= move.cra_retention_percent <= 100 or not 0 <= move.cra_recovery_percent <= 100 or move.cra_fixed_recovery < 0:
                raise ValidationError(_('Percentages must be between 0 and 100 and fixed recovery must be nonnegative.'))

    def _cra_receivable(self):
        self.ensure_one()
        lines = self.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
        if not lines or len(lines.account_id) != 1:
            raise UserError(_('Contract documents require one receivable account; multiple payment-term lines are supported.'))
        return lines

    def _cra_record(self, kind, move, amount, retention=None, advance=None, original=None):
        self.ensure_one()
        return self.env['contract.movement'].with_context(cra_internal=INTERNAL).create({
            'kind': kind, 'move_id': move.id, 'invoice_id': self.id, 'amount': amount, 'cycle': self.cra_cycle,
            'company_amount': sum(move.line_ids.mapped('debit')),
            'retention_id': retention.id if retention else False,
            'advance_id': advance.id if advance else False,
            'original_id': original.id if original else False, 'project_id': (self.reversed_entry_id.cra_project_id if self.move_type == 'out_refund' else self.cra_project_id).id,
        })

    def _cra_post_invoice(self):
        self.ensure_one()
        if self.cra_movement_ids.filtered(lambda m: m.cycle == self.cra_cycle):
            raise UserError(_('This invoice already has contract accounting movements in this posting cycle.'))
        company = self.company_id
        company._cra_check_config()
        self._check_cra_percent()
        if len(self.invoice_line_ids.sale_line_ids.order_id) > 1:
            raise UserError(_('Split mixed-sales-order invoices before applying retention or recovery.'))
        if self.cra_base <= 0 or self.cra_payable < 0:
            raise UserError(_('Eligible net work must be positive and deductions cannot exceed the gross invoice.'))
        if self.cra_enabled and company.cra_due_policy == 'required' and not self.cra_handover_date:
            raise UserError(_('Enter the expected handover date.'))
        retention_amount = self.currency_id.round(self.cra_base * self.cra_retention_percent / 100) if self.cra_enabled else 0
        recovery_amount = (self.currency_id.round(self.cra_base * self.cra_recovery_percent / 100) if self.cra_recovery_method == 'percent'
                           else self.currency_id.round(self.cra_fixed_recovery)) if self.cra_advance_id else 0
        receivable = self._cra_receivable()
        if self.currency_id.compare_amounts(sum(receivable.mapped('amount_residual_currency')), retention_amount + recovery_amount) < 0:
            raise UserError(_('Insufficient unreconciled invoice receivable for the contract deductions.'))
        if self.cra_advance_id:
            advance = self.cra_advance_id
            if self.date < advance.date:
                raise UserError(_('A recovery cannot predate advance registration.'))
            if advance.registration_move_id.state != 'posted':
                raise UserError(_('Register the advance before recovering it.'))
            if (advance.company_id != company or advance.partner_id != self.commercial_partner_id or
                    advance.currency_id != self.currency_id or advance.project_id != self.cra_project_id):
                raise UserError(_('Advance customer, company, currency and project must match the invoice exactly.'))
            if self.currency_id.compare_amounts(recovery_amount, advance.remaining) > 0:
                raise UserError(_('Requested recovery exceeds the available advance. Reduce the amount.'))
            if self.currency_id.compare_amounts(recovery_amount, self.cra_base) > 0:
                raise UserError(_('Recovery cannot exceed eligible untaxed work.'))
        if not self.currency_id.is_zero(retention_amount):
            retention = self.cra_retention_ids
            if retention and retention.project_id != self.cra_project_id:
                raise UserError(_('The project cannot change when reposting an invoice with retention history.'))
            if not retention:
                retention = self.env['contract.retention'].with_context(cra_internal=INTERNAL).create({
                    'invoice_id': self.id, 'project_id': self.cra_project_id.id, 'handover_date': self.cra_handover_date})
            entry = transfer(self.env, company, company.cra_journal_id, self.currency_id, self.date,
                self.commercial_partner_id, company.cra_retention_account_id, receivable.account_id,
                retention_amount, _('Retention: %s', self.name))
            self._cra_record('hold', entry, retention_amount, retention=retention)
            reconcile(receivable | entry.line_ids.filtered(lambda l: l.account_id == receivable.account_id))
        if not self.currency_id.is_zero(recovery_amount):
            advance = self.cra_advance_id
            # Consume the historical liability at its carrying amount. Standard reconciliation
            # creates exchange differences against AR when invoice-date and payment-date rates differ.
            liability = advance.registration_move_id.line_ids.filtered(lambda l: l.account_id.account_type == 'liability_current')
            liability |= advance.movement_ids.filtered(lambda m: m.move_id.state == 'posted').move_id.line_ids.filtered(
                lambda l: l.account_id == liability.account_id and not l.reconciled)
            open_lines = liability.filtered(lambda l: not l.reconciled)
            carry_currency = -sum(open_lines.mapped('amount_residual_currency'))
            carry_balance = -sum(open_lines.mapped('amount_residual'))
            if self.currency_id.compare_amounts(carry_currency, recovery_amount) < 0:
                raise UserError(_('The advance liability was changed outside this module. Reconcile the discrepancy before recovering.'))
            balance = company.currency_id.round(carry_balance * recovery_amount / carry_currency)
            entry = transfer(self.env, company, company.cra_journal_id, self.currency_id, self.date,
                self.commercial_partner_id, liability.account_id, receivable.account_id,
                recovery_amount, _('Advance recovery: %s', self.name), debit_balance=balance, credit_balance=balance)
            self._cra_record('recovery', entry, recovery_amount, advance=advance)
            reconcile(open_lines | entry.line_ids.filtered(lambda l: l.account_id == liability.account_id))
            reconcile(receivable | entry.line_ids.filtered(lambda l: l.account_id == receivable.account_id))

    def _cra_post_credit(self):
        self.ensure_one()
        original = self.reversed_entry_id
        if original.move_type != 'out_invoice' or original.company_id != self.company_id or original.currency_id != self.currency_id or original.commercial_partner_id != self.commercial_partner_id:
            raise UserError(_('Credit note company, customer and currency must match the original invoice.'))
        original.company_id._cra_check_config()
        credits = original.reversal_move_ids.filtered(lambda m: m.state == 'posted' and m.move_type == 'out_refund')
        credited_base = self.currency_id.round(sum(credits.mapped('cra_base')))
        if self.cra_base <= 0 or self.currency_id.compare_amounts(credited_base, original.cra_base) > 0:
            raise UserError(_('Cumulative credit work cannot exceed the original eligible work.'))
        receivable = self._cra_receivable()
        for allocation in original.cra_movement_ids.filtered(lambda m: m.kind in ('hold', 'recovery') and m.move_id.state == 'posted' and m.cycle == original.cra_cycle):
            reversed_allocations = self.env['contract.movement'].search([('original_id', '=', allocation.id), ('move_id.state', '=', 'posted')])
            already = sum(reversed_allocations.mapped('amount'))
            target = self.currency_id.round(allocation.amount * credited_base / original.cra_base)
            if allocation.kind == 'hold':
                # Released retention stays in AR; only unreleased asset is corrected.
                target = min(target, self.currency_id.round(allocation.amount - allocation.retention_id.released))
            amount = self.currency_id.round(target - already)
            if amount <= 0:
                continue
            source_account = allocation.move_id.line_ids.filtered(lambda l: l.debit > 0).account_id
            if self.currency_id.compare_amounts(-sum(receivable.mapped('amount_residual_currency')), amount) < 0:
                raise UserError(_('Credit receivable is already reconciled; cannot reverse contract allocations.'))
            company = self.company_id
            carrying_balance = company.currency_id.round(
                allocation.company_amount * target / allocation.amount
                - sum(reversed_allocations.mapped('company_amount')))
            if allocation.kind == 'hold' and allocation.retention_id.released:
                asset_lines = allocation.retention_id.movement_ids.move_id.line_ids.filtered(
                    lambda l: l.account_id == source_account and not l.reconciled)
                asset_currency = sum(asset_lines.mapped('amount_residual_currency'))
                if self.currency_id.compare_amounts(asset_currency, amount) < 0:
                    raise UserError(_('Retention asset residual does not support this correction.'))
                carrying_balance = company.currency_id.round(sum(asset_lines.mapped('amount_residual')) * amount / asset_currency)
            entry = transfer(self.env, company, company.cra_journal_id, self.currency_id, self.date,
                self.commercial_partner_id, receivable.account_id, source_account, amount,
                _('Contract credit: %s', self.name), debit_balance=carrying_balance, credit_balance=carrying_balance)
            self._cra_record('hold_reverse' if allocation.kind == 'hold' else 'recovery_reverse', entry, amount,
                retention=allocation.retention_id, advance=allocation.advance_id, original=allocation)
            reconcile(receivable | entry.line_ids.filtered(lambda l: l.account_id == receivable.account_id))
            if allocation.kind == 'hold':
                source_lines = allocation.retention_id.movement_ids.move_id.line_ids.filtered(lambda l: l.account_id == source_account)
                reconcile(source_lines)
        # Only after the deduction reversals can the remaining credit safely offset original AR.
        super(AccountMove, original)._reconcile_reversed_moves(self, False)

    def _post(self, soft=True):
        candidates = self.filtered(lambda m: m.state == 'draft' and (m.cra_enabled or m.cra_advance_id or m.reversed_entry_id.cra_movement_ids))
        if candidates:
            require_accountant(self.env)
            lock(candidates | candidates.reversed_entry_id)
            advances = candidates.cra_advance_id | candidates.reversed_entry_id.cra_movement_ids.advance_id
            lock(advances)
            lock(candidates.reversed_entry_id.cra_retention_ids)
        posted = super()._post(soft=soft)
        for move in posted & candidates:
            if move.move_type == 'out_invoice':
                move._cra_post_invoice()
            elif move.move_type == 'out_refund' and move.reversed_entry_id.cra_movement_ids:
                move._cra_post_credit()
            else:
                raise UserError(_('Contract deductions only apply to customer progress invoices and linked credit notes.'))
        return posted

    def _reconcile_reversed_moves(self, reverse_moves, move_reverse_cancel):
        # Core calls this during posting; defer managed credit matching until transfers exist.
        for move, reverse in zip(self, reverse_moves):
            if not move.cra_movement_ids:
                super(AccountMove, move)._reconcile_reversed_moves(reverse, move_reverse_cancel)
        return reverse_moves

    def _reverse_moves(self, default_values_list=None, cancel=False):
        if self.cra_source_advance_ids.filtered('registration_move_id'):
            raise UserError(_('Refunding or reversing a registered advance requires an approved exceptional correction.'))
        if any(self.mapped('cra_protected')):
            raise UserError(_('Contract transfers cannot be reversed independently of their audit records.'))
        if cancel and self.cra_movement_ids:
            raise UserError(_('Use a regular credit note, not Reverse and Create Invoice / cancellation, for contract invoices.'))
        return super()._reverse_moves(default_values_list, cancel)

    def button_draft(self):
        if self.cra_source_advance_ids.filtered('registration_move_id'):
            raise UserError(_('The original registered advance payment cannot be reset.'))
        if any(self.mapped('cra_protected')):
            raise UserError(_('Protected transfers cannot be reset independently.'))
        managed = self.filtered(lambda m: m.state == 'posted' and m.cra_movement_ids)
        if managed:
            require_accountant(self.env)
            lock(managed)
            lock(managed.cra_movement_ids.advance_id)
            lock(managed.cra_retention_ids)
        for invoice in managed:
            if invoice.move_type != 'out_invoice':
                raise UserError(_('Contract credit notes cannot be reset; use an approved correction.'))
            if invoice.reversal_move_ids.filtered(lambda m: m.state == 'posted'):
                raise UserError(_('Reset is blocked while a posted linked credit exists.'))
            if any(invoice.cra_retention_ids.mapped('released')):
                raise UserError(_('Reset is blocked after retention release.'))
            allocations = invoice.cra_movement_ids.filtered(
                lambda m: m.cycle == invoice.cra_cycle and m.kind in ('hold', 'recovery'))
            lock(allocations.move_id)
            ar = invoice._cra_receivable()
            partials = ar.matched_debit_ids | ar.matched_credit_ids
            counterparts = partials.debit_move_id | partials.credit_move_id
            if (counterparts.move_id - invoice - allocations.move_id):
                raise UserError(_('Reset is blocked when customer payments or external reconciliations exist.'))
            # Reopen only this cycle's allocations; retain all posted audit history.
            allocations.move_id.line_ids.with_context(cra_internal=INTERNAL).remove_move_reconcile()
            for allocation in allocations:
                source = allocation.move_id.line_ids.filtered(lambda l: l.debit > 0).account_id
                entry = transfer(self.env, invoice.company_id, invoice.company_id.cra_journal_id,
                    invoice.currency_id, invoice.date, invoice.commercial_partner_id,
                    ar.account_id, source, allocation.amount, _('Contract reset: %s', invoice.name),
                    debit_balance=allocation.company_amount, credit_balance=allocation.company_amount)
                invoice._cra_record('hold_reset' if allocation.kind == 'hold' else 'recovery_reset',
                    entry, allocation.amount, retention=allocation.retention_id,
                    advance=allocation.advance_id, original=allocation)
                reconcile(allocation.move_id.line_ids | entry.line_ids)
            invoice.with_context(cra_internal=INTERNAL).write({'cra_cycle': invoice.cra_cycle + 1})
        return super(AccountMove, self.with_context(cra_internal=INTERNAL)).button_draft()

    def button_cancel(self):
        if self.cra_source_advance_ids.filtered('registration_move_id'):
            raise UserError(_('The original registered advance payment cannot be cancelled.'))
        if any(self.mapped('cra_protected')) or self.cra_movement_ids:
            raise UserError(_('Contract entries must be corrected by credit note, not cancellation.'))
        return super().button_cancel()

    @api.model_create_multi
    def create(self, values):
        if self.env.context.get('cra_internal') is not INTERNAL and any(v.get('cra_protected') or v.get('cra_cycle') for v in values):
            raise AccessError(_('Protected entries are created by contract actions only.'))
        return super().create(values)

    def write(self, vals):
        if {'date', 'journal_id', 'company_id', 'currency_id', 'line_ids'}.intersection(vals) or vals.get('state') in ('draft', 'cancel'):
            if self.cra_source_advance_ids.filtered('registration_move_id'):
                raise AccessError(_('Registered advance source entries cannot be changed.'))
        contract_fields = {'cra_enabled', 'cra_retention_percent', 'cra_handover_date', 'cra_advance_id',
                           'cra_recovery_method', 'cra_recovery_percent', 'cra_fixed_recovery'}
        if any(m.cra_protected and m.state == 'posted' for m in self) and {'date', 'journal_id', 'company_id', 'currency_id', 'line_ids', 'ref'}.intersection(vals):
            raise AccessError(_('Posted contract transfers cannot be edited.'))
        if contract_fields.intersection(vals) and any(m.state == 'posted' for m in self):
            raise AccessError(_('Posted contract inputs cannot be edited.'))
        if 'cra_handover_date' in vals and self.cra_retention_ids and any(m.cra_handover_date != fields.Date.to_date(vals['cra_handover_date']) for m in self):
            raise AccessError(_('The handover date cannot change after retention history exists.'))
        if 'cra_cycle' in vals and self.env.context.get('cra_internal') is not INTERNAL:
            raise AccessError(_('Posting cycles are controlled by contract actions.'))
        if {'partner_id', 'company_id', 'currency_id', 'invoice_line_ids', 'line_ids'}.intersection(vals) and self.cra_movement_ids and self.env.context.get('cra_internal') is not INTERNAL:
            for invoice in self:
                if any(k in vals and vals[k] != invoice[k].id for k in ('partner_id', 'company_id', 'currency_id')):
                    raise AccessError(_('Customer, company and currency cannot change after contract history exists.'))
        if 'cra_protected' in vals and self.env.context.get('cra_internal') is not INTERNAL:
            raise AccessError(_('Contract protection cannot be edited.'))
        if vals.get('state') in ('draft', 'cancel') and any(m.cra_protected or m.cra_movement_ids for m in self) and self.env.context.get('cra_internal') is not INTERNAL:
            raise UserError(_('Reset or cancellation of contract accounting is blocked.'))
        return super().write(vals)

    def unlink(self):
        if self.cra_source_advance_ids.filtered('registration_move_id'):
            raise AccessError(_('Registered advance source entries cannot be deleted.'))
        if any(self.mapped('cra_protected')) or self.cra_movement_ids:
            raise AccessError(_('Contract accounting history cannot be deleted.'))
        return super().unlink()


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def write(self, values):
        financial = {'account_id', 'partner_id', 'currency_id', 'amount_currency', 'debit', 'credit',
                     'balance', 'date', 'move_id', 'tax_ids', 'tax_line_id', 'name'}
        if financial.intersection(values) and self.move_id.cra_source_advance_ids.filtered('registration_move_id'):
            raise AccessError(_('Registered advance source journal items cannot be edited.'))
        if financial.intersection(values) and any(l.move_id.cra_protected and l.move_id.state == 'posted' for l in self):
            raise AccessError(_('Posted contract transfer lines cannot be changed.'))
        return super().write(values)

    def unlink(self):
        if self.move_id.cra_source_advance_ids.filtered('registration_move_id'):
            raise AccessError(_('Registered advance source journal items cannot be deleted.'))
        if any(l.move_id.cra_protected for l in self):
            raise AccessError(_('Contract transfer journal items cannot be deleted.'))
        return super().unlink()

    def reconcile(self):
        if self.env.context.get('cra_internal') is not INTERNAL and any(
            l.move_id.cra_protected and l.account_id.account_type != 'asset_receivable' for l in self):
            raise AccessError(_('Retention and liability matching must use contract actions.'))
        return super().reconcile()

    def remove_move_reconcile(self):
        counterparts = self.matched_debit_ids.debit_move_id | self.matched_credit_ids.credit_move_id
        if self.env.context.get('cra_internal') is not INTERNAL and any(l.move_id.cra_protected for l in self | counterparts):
            raise UserError(_('Unreconciling a contract allocation is blocked; use a linked credit note.'))
        return super().remove_move_reconcile()


class PartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    def unlink(self):
        if self.env.context.get('cra_internal') is not INTERNAL and any(
                l.move_id.cra_protected for l in self.debit_move_id | self.credit_move_id):
            raise AccessError(_('Contract allocation reconciliations cannot be removed independently.'))
        return super().unlink()

    def write(self, values):
        if {'debit_move_id', 'credit_move_id', 'amount', 'debit_amount_currency', 'credit_amount_currency'}.intersection(values) and any(
                l.move_id.cra_protected for l in self.debit_move_id | self.credit_move_id):
            raise AccessError(_('Contract allocation reconciliation amounts cannot be edited.'))
        return super().write(values)


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    def action_contract_advance(self):
        self.ensure_one()
        require_accountant(self.env)
        advance = self.env['contract.advance'].search([('payment_id', '=', self.id)], limit=1)
        if not advance:
            advance = self.env['contract.advance'].create({'payment_id': self.id})
        return {'type': 'ir.actions.act_window', 'res_model': 'contract.advance',
                'view_mode': 'form', 'res_id': advance.id}

    def write(self, vals):
        if {'amount', 'currency_id', 'partner_id', 'company_id', 'payment_type', 'partner_type', 'date', 'journal_id', 'state'}.intersection(vals):
            registered = self.env['contract.advance'].search([('payment_id', 'in', self.ids), ('registration_move_id', '!=', False)])
            # Computed in_process -> paid is a normal standard payment settlement transition.
            if registered and (set(vals) != {'state'} or vals.get('state') not in ('in_process', 'paid')):
                raise UserError(_('Registered advance payments cannot be changed or cancelled.'))
        return super().write(vals)


class Partner(models.Model):
    _inherit = 'res.partner'

    def action_release_contract_retention(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Release Retention'),
                'res_model': 'contract.retention.release.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_partner_id': self.commercial_partner_id.id}}
