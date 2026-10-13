from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class Company(models.Model):
    _inherit = 'res.company'
    cra_enabled = fields.Boolean(string='Contract retention and advances')
    cra_retention_percent = fields.Float(default=10)
    cra_recovery_percent = fields.Float(default=20)
    cra_retention_account_id = fields.Many2one('account.account', string='Retention current asset')
    cra_advance_account_id = fields.Many2one('account.account', string='Customer advance liability')
    cra_journal_id = fields.Many2one('account.journal', string='Contract transfer journal')
    cra_due_policy = fields.Selection([('optional', 'Optional handover date'),
                                      ('required', 'Require handover date')], default='optional', required=True)

    @api.constrains('cra_retention_percent', 'cra_recovery_percent', 'cra_retention_account_id',
                    'cra_advance_account_id', 'cra_journal_id')
    def _cra_validate(self):
        for company in self:
            if not 0 <= company.cra_retention_percent <= 100 or not 0 <= company.cra_recovery_percent <= 100:
                raise ValidationError(_('Percentages must be between 0 and 100.'))
            for account, kind in [(company.cra_retention_account_id, 'asset_current'),
                                  (company.cra_advance_account_id, 'liability_current')]:
                if account and (company not in account.company_ids or account.account_type != kind or not account.reconcile):
                    raise ValidationError(_('Use a reconcilable current asset for retention and current liability for advances, in this company.'))
            if company.cra_journal_id and (company.cra_journal_id.company_id != company or company.cra_journal_id.type != 'general'):
                raise ValidationError(_('The contract journal must be a general journal in this company.'))
            if company.cra_retention_account_id and company.cra_retention_account_id == company.cra_advance_account_id:
                raise ValidationError(_('Retention and advance accounts must be different.'))

    def _cra_check_config(self):
        self.ensure_one()
        self._cra_validate()
        if not self.cra_enabled or not self.cra_retention_account_id or not self.cra_advance_account_id or not self.cra_journal_id:
            raise ValidationError(_('Enable and configure contract accounts and journal in Accounting settings.'))


class Settings(models.TransientModel):
    _inherit = 'res.config.settings'
    cra_enabled = fields.Boolean(related='company_id.cra_enabled', readonly=False)
    cra_retention_percent = fields.Float(related='company_id.cra_retention_percent', readonly=False)
    cra_recovery_percent = fields.Float(related='company_id.cra_recovery_percent', readonly=False)
    cra_retention_account_id = fields.Many2one(related='company_id.cra_retention_account_id', readonly=False)
    cra_advance_account_id = fields.Many2one(related='company_id.cra_advance_account_id', readonly=False)
    cra_journal_id = fields.Many2one(related='company_id.cra_journal_id', readonly=False)
    cra_due_policy = fields.Selection(related='company_id.cra_due_policy', readonly=False)
