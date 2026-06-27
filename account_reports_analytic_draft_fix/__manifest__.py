{
    'name': "Account Reports - Analytic Groupby Draft Entries Fix",
    'summary': "Make the 'Analytic Group By' columns of financial reports (P&L, Balance Sheet, ...) "
               "respect the 'Include Draft Entries' option instead of always showing posted entries only.",
    'description': """
Odoo only creates account.analytic.line records when a journal entry is posted (see
account.move._post()), and deletes them when the entry is reset to draft (see
account.move.button_draft()). Because the "Analytic Group By" columns on financial reports
(P&L, Balance Sheet, ...) are built exclusively from account.analytic.line, draft journal
entries never appear in those columns, even when the report's "Include Draft Entries" option
is enabled.

This module overrides account.report._prepare_lines_for_analytic_groupby to also synthesize,
directly in SQL, the missing analytic rows for draft account.move.line records that have an
analytic_distribution but no analytic.line yet - exploding their JSON distribution the same way
account.move.line._prepare_analytic_distribution_line does when posting (without its rounding
adjustment for accounts split across multiple distribution keys on the same line, an acceptable
approximation for reporting). Posted entries keep going through the original, unmodified path.
""",
    'version': '18.0.1.0.0',
    'category': 'Accounting/Accounting',
    'author': 'Mahmoud Ahmed',
    'license': 'LGPL-3',
    'depends': ['account_reports'],
    'installable': True,
    'auto_install': False,
}
