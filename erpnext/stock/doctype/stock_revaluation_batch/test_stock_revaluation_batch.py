# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.utils import add_days, getdate, nowdate

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	get_repost_item_valuation_entries,
)
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.tests.test_stock_ledger import freeze_rate, get_sle
from erpnext.tests.utils import ERPNextTestSuite

WAREHOUSE = "_Test Warehouse - _TC"
PERPETUAL_COMPANY = "_Test Company with perpetual inventory"


class TestStockRevaluationBatch(ERPNextTestSuite):
	def setUp(self):
		super().setUp()
		frappe.flags.dont_execute_stock_reposts = True

	def tearDown(self):
		frappe.flags.dont_execute_stock_reposts = False
		super().tearDown()

	def make_stale_ledger(self, item_name, valuation_method="Moving Average"):
		"""Receipt, issue, then a backdated receipt whose repost is left queued. Returns item and repost."""
		item = make_item(item_name, {"is_stock_item": 1, "valuation_method": valuation_method}).name
		make_stock_entry(
			item_code=item, target=WAREHOUSE, qty=10, basic_rate=100, posting_date=add_days(nowdate(), -5)
		)
		make_stock_entry(item_code=item, source=WAREHOUSE, qty=5, posting_date=add_days(nowdate(), -3))
		receipt = make_stock_entry(
			item_code=item, target=WAREHOUSE, qty=10, basic_rate=200, posting_date=add_days(nowdate(), -4)
		)
		reposts = frappe.get_all(
			"Repost Item Valuation",
			filters={"status": "Queued", "docstatus": 1},
			or_filters={"voucher_no": receipt.name, "item_code": item},
			pluck="name",
		)
		return item, reposts[0]

	def make_batch(self, repost):
		batch = frappe.get_doc(
			{
				"doctype": "Stock Revaluation Batch",
				"company": "_Test Company",
				"from_date": add_days(nowdate(), -30),
				"upto_posting_date": nowdate(),
			}
		).insert()
		batch.items = [row for row in batch.items if row.repost_item_valuation == repost]
		batch.rates = []
		return batch

	def issue_stock_value(self, item):
		return frappe.db.get_value(
			"Stock Ledger Entry", {"item_code": item, "actual_qty": -5, "is_cancelled": 0}, "stock_value"
		)

	def test_periodic_average_reposts_wait_for_the_batch(self):
		_item, held = self.make_stale_ledger("_Test Held Repost Item", "Periodic Average")
		_item, scheduled = self.make_stale_ledger("_Test Scheduled Repost Item")
		scheduler_queue = [entry.name for entry in get_repost_item_valuation_entries()]
		self.assertNotIn(held, scheduler_queue)
		self.assertIn(scheduled, scheduler_queue)

	def test_preview_shows_difference_without_changing_the_ledger(self):
		item, repost = self.make_stale_ledger("_Test Revaluation Preview Item")
		batch = self.make_batch(repost)
		row = batch.items[0]
		self.assertEqual(row.posting_date, getdate(add_days(nowdate(), -4)))
		self.assertEqual(row.current_stock_value, 2500)

		batch.preview_revaluation()
		self.assertEqual(row.revalued_stock_value, 2250)
		self.assertEqual(row.value_difference, -250)
		self.assertEqual(self.issue_stock_value(item), 500)

	def test_submit_posts_the_queued_reposts(self):
		item, repost = self.make_stale_ledger("_Test Revaluation Post Item")
		batch = self.make_batch(repost)
		batch.submit()

		self.assertEqual(frappe.db.get_value("Repost Item Valuation", repost, "status"), "Completed")
		self.assertEqual(self.issue_stock_value(item), 2250)

	def test_period_valuation_reprices_issues_and_closing_stock(self):
		item = make_item(
			"_Test Period Valuation Item", {"is_stock_item": 1, "valuation_method": "Periodic Average"}
		).name
		freeze_rate(item, PERPETUAL_COMPANY, 100)
		stores, finished = "Stores - TCP1", "Finished Goods - TCP1"
		receipts = [
			make_stock_entry(
				item_code=item,
				target=stores,
				qty=10,
				basic_rate=100,
				company=PERPETUAL_COMPANY,
				posting_date=add_days(nowdate(), -20),
			),
			make_stock_entry(
				item_code=item,
				target=finished,
				qty=10,
				basic_rate=200,
				company=PERPETUAL_COMPANY,
				posting_date=add_days(nowdate(), -10),
			),
		]
		issue = make_stock_entry(
			item_code=item,
			source=stores,
			qty=5,
			company=PERPETUAL_COMPANY,
			posting_date=add_days(nowdate(), -5),
		)
		self.assertEqual(get_sle(issue).stock_value_difference, -500)

		batch = frappe.get_doc(
			{
				"doctype": "Stock Revaluation Batch",
				"company": PERPETUAL_COMPANY,
				"from_date": add_days(nowdate(), -30),
				"upto_posting_date": nowdate(),
			}
		).insert()
		row = next(rate for rate in batch.rates if rate.item_code == item)
		self.assertEqual(row.current_rate, 100)
		self.assertEqual((row.receipt_qty, row.receipt_value), (20, 3000))
		self.assertEqual(row.period_rate, 150)
		self.assertEqual((row.issue_qty, row.closing_qty), (5, 15))
		self.assertEqual(row.cogs_adjustment, 250)
		self.assertEqual(row.stock_adjustment, -250)

		batch.items, batch.rates = [], [row]
		batch.submit()

		self.assertEqual(
			frappe.db.get_value(
				"Item Valuation Rate", {"item_code": item, "company": PERPETUAL_COMPANY}, "valuation_rate"
			),
			150,
		)
		self.assertEqual(get_sle(issue).stock_value_difference, -750)
		revaluations = frappe.get_all(
			"Stock Ledger Entry",
			{"voucher_no": batch.name, "is_cancelled": 0},
			["warehouse", "qty_after_transaction", "valuation_rate", "stock_value_difference"],
			order_by="warehouse",
		)
		self.assertEqual(
			[
				(r.warehouse, r.qty_after_transaction, r.valuation_rate, r.stock_value_difference)
				for r in revaluations
			],
			[(finished, 10, 150, -500), (stores, 5, 150, 500)],
		)
		vouchers = [voucher.name for voucher in [*receipts, issue, batch]]
		self.assertEqual(get_stock_account_balance(vouchers), 2250)


def get_stock_account_balance(vouchers):
	from frappe.query_builder.functions import Sum

	gl, account = frappe.qb.DocType("GL Entry"), frappe.qb.DocType("Account")
	balance = (
		frappe.qb.from_(gl)
		.join(account)
		.on(gl.account == account.name)
		.select(Sum(gl.debit) - Sum(gl.credit))
		.where(gl.voucher_no.isin(vouchers) & (gl.is_cancelled == 0) & (account.account_type == "Stock"))
		.run()
	)
	return balance[0][0]
