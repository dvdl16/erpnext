# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import flt

from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import execute_reposting_entry
from erpnext.stock.stock_ledger import get_items_to_be_repost, update_entries_after


class StockRevaluationBatch(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from erpnext.stock.doctype.stock_revaluation_batch_item.stock_revaluation_batch_item import StockRevaluationBatchItem
		from frappe.types import DF

		amended_from: DF.Link | None
		company: DF.Link
		items: DF.Table[StockRevaluationBatchItem]
		total_current_stock_value: DF.Currency
		total_revalued_stock_value: DF.Currency
		total_value_difference: DF.Currency
		upto_posting_date: DF.Date
	# end: auto-generated types

	"""Previews and posts the Repost Item Valuation entries held back by Disable Automatic Revaluation Posting."""

	def validate(self):
		if not self.items:
			self.fetch_items()
		self.set_totals()

	@frappe.whitelist()
	def fetch_items(self):
		"""One row per repost queued for the company up to the posting date."""
		reposts = frappe.get_all(
			"Repost Item Valuation",
			{
				"company": self.company,
				"docstatus": 1,
				"status": "Queued",
				"posting_date": ("<=", self.upto_posting_date),
			},
			[
				"name",
				"based_on",
				"item_code",
				"warehouse",
				"voucher_type",
				"voucher_no",
				"posting_date",
				"posting_time",
			],
			order_by="posting_date, posting_time, creation",
		)
		self.items = []
		for repost in reposts:
			item = self.append("items", {"repost_item_valuation": repost.pop("name"), **repost})
			item.current_stock_value = self.get_posted_value(item)
		self.set_totals()

	@frappe.whitelist()
	def preview_revaluation(self):
		"""Run every repost inside a savepoint, record the resulting values, then roll back.

		The difference is what posting the batch adds to the stock account."""
		frappe.db.savepoint("revaluation_preview")
		try:
			for item in self.items:
				item.current_stock_value = self.get_posted_value(item)
				self.repost_in_place(item)
				item.revalued_stock_value = self.get_posted_value(item)
				item.value_difference = item.revalued_stock_value - item.current_stock_value
		finally:
			frappe.db.rollback(save_point="revaluation_preview")
		self.set_totals()

	def repost_in_place(self, item):
		repost = frappe.get_doc("Repost Item Valuation", item.repost_item_valuation)
		for target in get_repost_targets(repost):
			update_entries_after(
				{
					"item_code": target.item_code,
					"warehouse": target.warehouse,
					"posting_date": target.posting_date,
					"posting_time": target.posting_time,
				},
				allow_zero_rate=repost.allow_zero_rate,
				allow_negative_stock=repost.allow_negative_stock,
				via_landed_cost_voucher=repost.via_landed_cost_voucher,
			)

	def get_posted_value(self, item):
		"""Sum of stock value differences the repost's items and warehouses carry: the stock account side."""
		sle = frappe.qb.DocType("Stock Ledger Entry")
		value = 0.0
		for target in get_repost_targets(item):
			posted = (
				frappe.qb.from_(sle)
				.select(Sum(sle.stock_value_difference))
				.where(
					(sle.item_code == target.item_code)
					& (sle.warehouse == target.warehouse)
					& (sle.is_cancelled == 0)
				)
			).run()
			value += flt(posted[0][0])
		return value

	def set_totals(self):
		self.total_current_stock_value = sum(flt(item.current_stock_value) for item in self.items)
		self.total_revalued_stock_value = sum(flt(item.revalued_stock_value) for item in self.items)
		self.total_value_difference = sum(flt(item.value_difference) for item in self.items)

	def on_submit(self):
		if not self.items:
			frappe.throw(_("No queued reposts to post"))
		frappe.enqueue(
			post_reposts,
			batch=self.name,
			queue="long",
			timeout=21600,
			job_id=f"stock_revaluation_batch::{self.name}",
			now=frappe.in_test,
		)


def get_repost_targets(repost):
	"""Item and warehouse pairs a repost recalculates, each from its own posting datetime."""
	if repost.based_on == "Item and Warehouse":
		return [repost]
	return [frappe._dict(row) for row in get_items_to_be_repost(repost.voucher_type, repost.voucher_no)]


def post_reposts(batch):
	"""Run the batch's reposts in row order, whatever the automatic posting setting says."""
	names = frappe.get_all(
		"Stock Revaluation Batch Item", {"parent": batch}, pluck="repost_item_valuation", order_by="idx"
	)
	for name in names:
		execute_reposting_entry(name)
