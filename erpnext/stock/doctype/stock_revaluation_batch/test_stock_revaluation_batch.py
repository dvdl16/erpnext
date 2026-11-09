# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.utils import add_days, getdate, nowdate

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	get_repost_item_valuation_entries,
)
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.tests.utils import ERPNextTestSuite

WAREHOUSE = "_Test Warehouse - _TC"


class TestStockRevaluationBatch(ERPNextTestSuite):
	def setUp(self):
		super().setUp()
		frappe.flags.dont_execute_stock_reposts = True

	def tearDown(self):
		frappe.flags.dont_execute_stock_reposts = False
		super().tearDown()

	def make_stale_ledger(self, item_name):
		"""Receipt, issue, then a backdated receipt whose repost is left queued. Returns item and repost."""
		item = make_item(item_name, {"is_stock_item": 1, "valuation_method": "Moving Average"}).name
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
			{"doctype": "Stock Revaluation Batch", "company": "_Test Company", "upto_posting_date": nowdate()}
		).insert()
		batch.items = [row for row in batch.items if row.repost_item_valuation == repost]
		return batch

	def issue_stock_value(self, item):
		return frappe.db.get_value(
			"Stock Ledger Entry", {"item_code": item, "actual_qty": -5, "is_cancelled": 0}, "stock_value"
		)

	@ERPNextTestSuite.change_settings("Stock Settings", {"disable_automatic_revaluation_posting": 1})
	def test_setting_keeps_queued_reposts_out_of_the_scheduler(self):
		self.make_stale_ledger("_Test Revaluation Setting Item")
		self.assertTrue(frappe.db.exists("Repost Item Valuation", {"status": "Queued", "docstatus": 1}))
		self.assertEqual(get_repost_item_valuation_entries(), [])

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
