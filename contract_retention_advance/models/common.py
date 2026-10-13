from odoo import _, Command
from odoo.exceptions import UserError, AccessError

# Identity sentinel: RPC context values cannot impersonate internal creation.
INTERNAL = object()


def require_accountant(env):
    if not env.user.has_group('account.group_account_user'):
        raise AccessError(env._('An accountant is required for this operation.'))


def lock(records):
    records.check_access('write')
    records.flush_recordset()
    if records:
        # Fixed ORM-owned table name; parameterized identifiers and values.
        from odoo.tools import SQL
        records.env.cr.execute(SQL('SELECT id FROM %s WHERE id IN %s ORDER BY id FOR UPDATE',
                                   SQL.identifier(records._table), tuple(sorted(records.ids))))
        records.invalidate_recordset()


def transfer(env, company, journal, currency, date, partner, debit_account, credit_account,
             amount, reference, debit_balance=None, credit_balance=None):
    require_accountant(env)
    if company not in env.companies:
        raise AccessError(env._('Company is not active.'))
    if journal.company_id != company or journal.type != 'general':
        raise UserError(env._('Configure a general journal belonging to this company.'))
    if journal.currency_id and journal.currency_id != currency:
        raise UserError(env._('The transfer journal currency is incompatible with the document.'))
    for account in debit_account | credit_account:
        if company not in account.company_ids:
            raise UserError(env._('Transfer account does not belong to the company.'))
    amount = currency.round(amount)
    if currency.compare_amounts(amount, 0) <= 0:
        raise UserError(env._('Transfer amount must be positive.'))
    balance = currency._convert(amount, company.currency_id, company, date)
    db = balance if debit_balance is None else debit_balance
    cb = balance if credit_balance is None else credit_balance
    if db <= 0 or cb <= 0:
        raise UserError(env._('The company-currency transfer rounds to zero. Increase the amount or combine the release.'))
    if not company.currency_id.is_zero(db - cb):
        raise UserError(env._('The transfer requires balanced company-currency amounts.'))
    values = []
    for account, sign, value in [(debit_account, 1, db), (credit_account, -1, cb)]:
        values.append(Command.create({
            'name': reference, 'account_id': account.id, 'partner_id': partner.id,
            'currency_id': currency.id, 'amount_currency': sign * amount,
            'debit': value if sign > 0 else 0, 'credit': value if sign < 0 else 0,
            'date_maturity': date,
        }))
    move = env['account.move'].with_company(company).with_context(cra_internal=INTERNAL).create({
        'move_type': 'entry', 'company_id': company.id, 'journal_id': journal.id,
        'date': date, 'ref': reference, 'cra_protected': True, 'line_ids': values,
    })
    move.action_post()
    # Core can move locked entries forward: fail rather than silently changing a requested date.
    if move.date != date:
        raise UserError(env._('The requested date is locked. Choose an open accounting date.'))
    return move


def reconcile(lines):
    for account in lines.account_id:
        relevant = lines.filtered(lambda l: l.account_id == account and not l.reconciled)
        if relevant and any(l.balance > 0 for l in relevant) and any(l.balance < 0 for l in relevant):
            relevant.with_context(cra_internal=INTERNAL).reconcile()
