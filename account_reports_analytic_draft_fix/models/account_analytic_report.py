from odoo import models, api
from odoo.tools import SQL


class AccountReport(models.AbstractModel):
    _inherit = 'account.report'

    @api.model
    def _prepare_lines_for_analytic_groupby(self):
        """Override of account_reports.

        Odoo only creates `account.analytic.line` records when a journal entry is posted
        (see `account.move._post()`), and deletes them when the entry is reset to draft
        (see `account.move.button_draft()`). Because of that, the original method - which
        only reads from `account_analytic_line` - never has any data to show for draft
        entries, so the "Analytic Group By" columns on financial reports (P&L, Balance
        Sheet, ...) always show posted entries only, even when "Include Draft Entries" is
        enabled on the report.

        This override keeps the original behavior for posted entries, then adds a second
        INSERT that synthesizes the missing analytic rows directly from draft
        `account_move_line` records that have an `analytic_distribution` but no
        `account.analytic.line` yet, by exploding their JSON distribution in SQL (mirroring
        `account.move.line._prepare_analytic_distribution_line()`, without the rounding
        adjustment it does when the same analytic account is split across several
        distribution keys on the same line, which is an acceptable approximation for
        reporting purposes).
        """
        self.env.cr.execute("SELECT 1 FROM information_schema.tables WHERE table_name='analytic_temp_account_move_line'")
        if self.env.cr.fetchone():
            return

        project_plan, other_plans = self.env['account.analytic.plan']._get_all_plans()
        plans = project_plan + other_plans
        analytic_cols = SQL(", ").join(SQL('"account_analytic_line".%s', SQL.identifier(n._column_name())) for n in plans)
        analytic_distribution_equivalent = SQL('to_jsonb(UNNEST(ARRAY[%s]))', analytic_cols)

        change_equivalence_dict = {
            'id': SQL("account_analytic_line.id"),
            'balance': SQL("-amount"),
            'display_type': 'product',
            'parent_state': 'posted',
            'account_id': SQL.identifier("general_account_id"),
            'debit': SQL("CASE WHEN (amount < 0) THEN -amount else 0 END"),
            'credit': SQL("CASE WHEN (amount > 0) THEN amount else 0 END"),
            'analytic_distribution': analytic_distribution_equivalent,
            'date': SQL("account_analytic_line.date"),
            'company_id': SQL("account_analytic_line.company_id"),
        }

        all_stored_aml_fields = {
            field
            for field, attrs in self.env['account.move.line'].fields_get().items()
            if attrs['type'] not in ['many2many', 'one2many'] and attrs.get('store')
        }

        for aml_field in all_stored_aml_fields:
            if aml_field not in change_equivalence_dict:
                change_equivalence_dict[aml_field] = SQL('"account_move_line".%s', SQL.identifier(aml_field))

        stored_aml_fields, fields_to_insert = self.env['account.move.line']._prepare_aml_shadowing_for_report(change_equivalence_dict)

        # --- Synthetic rows for draft move lines (no account.analytic.line exists for them yet) ---
        plan_col_alias = lambda plan: SQL.identifier(f"plan_col_{plan.id}")
        plan_pivot_cols = SQL(", ").join(
            SQL(
                "MAX(account_analytic_account.id) FILTER (WHERE account_analytic_account.root_plan_id = %(plan_id)s) AS %(alias)s",
                plan_id=plan.id,
                alias=plan_col_alias(plan),
            )
            for plan in plans
        )
        plan_pivot_select_cols = SQL(", ").join(SQL("draft_distribution.%s", plan_col_alias(plan)) for plan in plans)

        draft_change_equivalence_dict = dict(change_equivalence_dict)
        draft_balance_expr = SQL('"account_move_line".balance * draft_distribution.percentage / 100.0')
        draft_change_equivalence_dict.update({
            'id': SQL('-"account_move_line".id'),
            'balance': draft_balance_expr,
            'display_type': 'product',
            'parent_state': 'draft',
            'account_id': SQL('"account_move_line".account_id'),
            'debit': SQL("CASE WHEN (%(balance)s) > 0 THEN (%(balance)s) ELSE 0 END", balance=draft_balance_expr),
            'credit': SQL("CASE WHEN (%(balance)s) < 0 THEN -(%(balance)s) ELSE 0 END", balance=draft_balance_expr),
            'analytic_distribution': SQL('to_jsonb(UNNEST(ARRAY[%s]))', plan_pivot_select_cols),
            'date': SQL('"account_move_line".date'),
            'company_id': SQL('"account_move_line".company_id'),
        })
        draft_stored_aml_fields, draft_fields_to_insert = self.env['account.move.line']._prepare_aml_shadowing_for_report(draft_change_equivalence_dict)

        query = SQL("""
            -- Create a temporary table, dropping not null constraints because we're not filling those columns
            CREATE TEMPORARY TABLE IF NOT EXISTS analytic_temp_account_move_line () inherits (account_move_line) ON COMMIT DROP;
            ALTER TABLE analytic_temp_account_move_line NO INHERIT account_move_line;
            ALTER TABLE analytic_temp_account_move_line DROP CONSTRAINT IF EXISTS account_move_line_check_amount_currency_balance_sign;
            ALTER TABLE analytic_temp_account_move_line ALTER COLUMN move_id DROP NOT NULL;
            ALTER TABLE analytic_temp_account_move_line ALTER COLUMN currency_id DROP NOT NULL;

            INSERT INTO analytic_temp_account_move_line (%(stored_aml_fields)s)
            SELECT %(fields_to_insert)s
            FROM account_analytic_line
            LEFT JOIN account_move_line
                ON account_analytic_line.move_line_id = account_move_line.id
            WHERE
                account_analytic_line.general_account_id IS NOT NULL;

            INSERT INTO analytic_temp_account_move_line (%(draft_stored_aml_fields)s)
            SELECT %(draft_fields_to_insert)s
            FROM (
                SELECT
                    draft_keys.move_line_id,
                    draft_keys.percentage,
                    %(plan_pivot_cols)s
                FROM (
                    SELECT
                        aml.id AS move_line_id,
                        kv.key AS dist_key,
                        kv.value::numeric AS percentage
                    FROM account_move_line aml
                    CROSS JOIN LATERAL jsonb_each_text(aml.analytic_distribution) AS kv(key, value)
                    WHERE
                        aml.parent_state = 'draft'
                        AND aml.analytic_distribution IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM account_analytic_line aal WHERE aal.move_line_id = aml.id
                        )
                ) draft_keys
                CROSS JOIN LATERAL regexp_split_to_table(draft_keys.dist_key, ',') AS account_id_txt
                JOIN account_analytic_account
                    ON account_analytic_account.id = account_id_txt::int
                GROUP BY draft_keys.move_line_id, draft_keys.dist_key, draft_keys.percentage
            ) draft_distribution
            JOIN account_move_line
                ON account_move_line.id = draft_distribution.move_line_id;

            -- Create a supporting index to avoid seq.scans
            CREATE INDEX IF NOT EXISTS analytic_temp_account_move_line__composite_idx ON analytic_temp_account_move_line (analytic_distribution, journal_id, date, company_id);
            -- Update statistics for correct planning
            ANALYZE analytic_temp_account_move_line
        """,
            stored_aml_fields=stored_aml_fields,
            fields_to_insert=fields_to_insert,
            draft_stored_aml_fields=draft_stored_aml_fields,
            draft_fields_to_insert=draft_fields_to_insert,
            plan_pivot_cols=plan_pivot_cols,
        )

        self.env.cr.execute(query)
