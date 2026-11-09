# Copyright (c) 2025, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.tests.utils import ERPNextTestSuite


class TestRepostQueue(ERPNextTestSuite):
	def test_entries_pop_in_posting_order_with_ties_in_insertion_order(self):
		from erpnext.stock.stock_ledger import RepostQueue

		def entry(name, posting_datetime, creation="2026-01-01 10:00:00"):
			return frappe._dict(name=name, posting_datetime=posting_datetime, creation=creation)

		queue = RepostQueue([entry("a", "2026-01-02 00:00:01"), entry("b", "2026-01-04 00:00:01")])
		queue.extend(
			[
				entry("c", "2026-01-01 00:00:01"),
				entry("d", "2026-01-02 00:00:01"),
				entry("e", "2026-01-02 00:00:01", creation="2026-01-01 09:00:00"),
			]
		)

		self.assertEqual([row.name for row in queue.peek(2)], ["c", "e"])
		self.assertEqual(len(queue), 5)
		self.assertEqual([queue.popleft().name for _ in range(len(queue))], ["c", "e", "a", "d", "b"])

	def test_repost_fetches_each_entry_once_when_dependants_join_the_queue(self):
		from unittest.mock import patch

		from frappe.utils import add_days, nowdate

		import erpnext.stock.stock_ledger as stock_ledger
		from erpnext.stock.doctype.item.test_item import make_item
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = make_item("_Test Repost Batch Fetch Item", {"is_stock_item": 1}).name
		source, target = "_Test Warehouse - _TC", "_Test Warehouse 1 - _TC"
		make_stock_entry(
			item_code=item, target=source, qty=10, basic_rate=100, posting_date=add_days(nowdate(), -3)
		)
		for days in (-2, -1):
			make_stock_entry(
				item_code=item, source=source, target=target, qty=2, posting_date=add_days(nowdate(), days)
			)

		fetched = []

		def record(names):
			fetched.extend(names)
			return get_sle_entries_by_names(names)

		get_sle_entries_by_names = stock_ledger.get_sle_entries_by_names
		with patch.object(stock_ledger, "get_sle_entries_by_names", side_effect=record):
			stock_ledger.update_entries_after(
				{
					"item_code": item,
					"warehouse": source,
					"posting_date": add_days(nowdate(), -3),
					"posting_time": "00:00:00",
				}
			)

		target_sles = frappe.get_all(
			"Stock Ledger Entry", {"item_code": item, "warehouse": target}, pluck="name"
		)
		self.assertTrue(set(target_sles) <= set(fetched))
		self.assertEqual(len(fetched), len(set(fetched)))


COMPANY = "_Test Company with perpetual inventory"


class TestSiteWideItemValuation(ERPNextTestSuite):
	@ERPNextTestSuite.change_settings("Stock Settings", {"site_wide_item_valuation": 1})
	def test_moving_average_pools_rate_across_warehouses(self):
		from erpnext.stock.doctype.item.test_item import make_item
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = make_item(
			"_Test Site Wide Valuation Item", {"is_stock_item": 1, "valuation_method": "Moving Average"}
		).name
		self.assert_pooled_rate(item)

	@ERPNextTestSuite.change_settings("Stock Settings", {"site_wide_item_valuation": 1})
	def test_batch_item_pools_rate_across_warehouses(self):
		from erpnext.stock.doctype.item.test_item import make_item

		item = make_item(
			"_Test Site Wide Valuation Batch Item",
			{
				"is_stock_item": 1,
				"valuation_method": "Moving Average",
				"has_batch_no": 1,
				"create_new_batch": 1,
				"batch_number_series": "SWVB-.#####",
			},
		).name
		self.assert_pooled_rate(item)

	def assert_pooled_rate(self, item):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
		from erpnext.stock.doctype.serial_and_batch_bundle.test_serial_and_batch_bundle import (
			get_batch_from_bundle,
		)

		first, second = "Stores - TCP1", "Finished Goods - TCP1"
		opening = make_stock_entry(item_code=item, target=first, qty=10, basic_rate=100, company=COMPANY)
		receipt = make_stock_entry(item_code=item, target=second, qty=10, basic_rate=200, company=COMPANY)
		bundle = opening.items[0].serial_and_batch_bundle
		issue = make_stock_entry(
			item_code=item,
			source=first,
			qty=5,
			company=COMPANY,
			batch_no=get_batch_from_bundle(bundle) if bundle else None,
		)

		def sle(voucher):
			return frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": voucher.name},
				["valuation_rate", "stock_value"],
				as_dict=True,
			)

		self.assertEqual(sle(receipt).valuation_rate, 150)
		self.assertEqual(sle(receipt).stock_value, 1500)
		self.assertEqual(sle(issue).valuation_rate, 150)
		self.assertEqual(sle(issue).stock_value, 750)
		issue.reload()
		self.assertEqual(issue.items[0].basic_rate, 150)

		# the receipt also revalues the idle warehouse, on the ledger, the Bin and the GL
		revaluation = frappe.db.get_value(
			"Stock Ledger Entry",
			{"voucher_no": receipt.name, "warehouse": first},
			["actual_qty", "qty_after_transaction", "valuation_rate", "stock_value_difference"],
			as_dict=True,
		)
		self.assertEqual(revaluation.actual_qty, 0)
		self.assertEqual(revaluation.qty_after_transaction, 10)
		self.assertEqual(revaluation.valuation_rate, 150)
		self.assertEqual(revaluation.stock_value_difference, 500)
		self.assertEqual(
			frappe.db.get_value("Bin", {"item_code": item, "warehouse": first}, "valuation_rate"), 150
		)
		self.assertEqual(get_stock_account_debit(receipt), 2000)

	@ERPNextTestSuite.change_settings("Stock Settings", {"site_wide_item_valuation": 1})
	def test_purchase_receipt_books_revaluation_without_divisional_loss(self):
		from erpnext.stock.doctype.item.test_item import make_item
		from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import make_purchase_receipt
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = make_item(
			"_Test Site Wide Valuation PR Item", {"is_stock_item": 1, "valuation_method": "Moving Average"}
		).name
		make_stock_entry(item_code=item, target="Stores - TCP1", qty=10, basic_rate=100, company=COMPANY)
		receipt = make_purchase_receipt(
			item_code=item,
			company=COMPANY,
			warehouse="Finished Goods - TCP1",
			supplier_warehouse="Work In Progress - TCP1",
			qty=10,
			rate=200,
		)

		self.assertEqual(get_stock_account_debit(receipt), 2000)
		loss_account = frappe.get_cached_value("Company", receipt.company, "default_expense_account")
		self.assertFalse(
			frappe.db.exists(
				"GL Entry", {"voucher_no": receipt.name, "account": loss_account, "is_cancelled": 0}
			)
		)


def get_stock_account_debit(voucher):
	from frappe.query_builder.functions import Sum

	gl, account = frappe.qb.DocType("GL Entry"), frappe.qb.DocType("Account")
	debit = (
		frappe.qb.from_(gl)
		.join(account)
		.on(gl.account == account.name)
		.select(Sum(gl.debit) - Sum(gl.credit))
		.where((gl.voucher_no == voucher.name) & (gl.is_cancelled == 0) & (account.account_type == "Stock"))
		.run()
	)
	return debit[0][0]

	def test_moving_average_stays_per_warehouse_by_default(self):
		from erpnext.stock.doctype.item.test_item import make_item
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = make_item(
			"_Test Per Warehouse Valuation Item", {"is_stock_item": 1, "valuation_method": "Moving Average"}
		).name
		make_stock_entry(item_code=item, target="_Test Warehouse - _TC", qty=10, basic_rate=100)
		receipt = make_stock_entry(item_code=item, target="_Test Warehouse 1 - _TC", qty=10, basic_rate=200)

		self.assertEqual(
			frappe.db.get_value("Stock Ledger Entry", {"voucher_no": receipt.name}, "valuation_rate"), 200
		)
