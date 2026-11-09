# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Order
from frappe.query_builder.functions import Min
from frappe.utils import flt, getdate

from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import execute_reposting_entry
from erpnext.stock.stock_ledger import update_entries_after


class StockRevaluationBatch(Document):
	"""Posts a period's stock revaluations as one batch of Repost Item Valuation entries."""

	def validate(self):
		if getdate(self.from_date) > getdate(self.to_date):
			frappe.throw(_("From Date cannot be after To Date"))
		if not self.items:
			self.fetch_items()
		self.set_totals()

	@frappe.whitelist()
	def fetch_items(self):
		"""One row per item and warehouse moved in the period, reposted from its first movement."""
		sle = frappe.qb.DocType("Stock Ledger Entry")
		rows = (
			frappe.qb.from_(sle)
			.select(sle.item_code, sle.warehouse, Min(sle.posting_datetime).as_("posting_datetime"))
			.where(
				(sle.company == self.company)
				& (sle.is_cancelled == 0)
				& (sle.posting_date >= self.from_date)
				& (sle.posting_date <= self.to_date)
			)
			.groupby(sle.item_code, sle.warehouse)
			.orderby(sle.item_code, sle.warehouse)
		).run(as_dict=True)

		self.items = []
		for row in rows:
			item = self.append(
				"items",
				{
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"posting_date": row.posting_datetime.date(),
					"posting_time": row.posting_datetime.time(),
				},
			)
			item.current_stock_value = self.get_stock_value(item)
		self.set_totals()

	@frappe.whitelist()
	def preview_revaluation(self):
		"""Repost every row inside a savepoint, record the resulting stock values, then roll back."""
		frappe.db.savepoint("revaluation_preview")
		try:
			for item in self.items:
				self.repost_in_place(item)
				item.revalued_stock_value = self.get_stock_value(item)
				item.value_difference = item.revalued_stock_value - item.current_stock_value
		finally:
			frappe.db.rollback(save_point="revaluation_preview")
		self.set_totals()

	def repost_in_place(self, item):
		update_entries_after(
			{
				"item_code": item.item_code,
				"warehouse": item.warehouse,
				"posting_date": item.posting_date,
				"posting_time": item.posting_time,
			},
			allow_zero_rate=self.allow_zero_rate,
			allow_negative_stock=self.allow_negative_stock,
		)

	def get_stock_value(self, item):
		sle = frappe.qb.DocType("Stock Ledger Entry")
		value = (
			frappe.qb.from_(sle)
			.select(sle.stock_value)
			.where(
				(sle.item_code == item.item_code)
				& (sle.warehouse == item.warehouse)
				& (sle.is_cancelled == 0)
			)
			.orderby(sle.posting_datetime, order=Order.desc)
			.orderby(sle.creation, order=Order.desc)
			.limit(1)
		).run()
		return flt(value[0][0]) if value else 0.0

	def set_totals(self):
		self.total_current_stock_value = sum(flt(item.current_stock_value) for item in self.items)
		self.total_revalued_stock_value = sum(flt(item.revalued_stock_value) for item in self.items)
		self.total_value_difference = sum(flt(item.value_difference) for item in self.items)

	def on_submit(self):
		if not self.items:
			frappe.throw(_("No stock movement in the period"))
		for item in self.items:
			repost = create_repost_item_valuation_entry(
				{
					"based_on": "Item and Warehouse",
					"item_code": item.item_code,
					"warehouse": item.warehouse,
					"posting_date": item.posting_date,
					"posting_time": item.posting_time,
					"company": self.company,
					"allow_negative_stock": self.allow_negative_stock,
					"allow_zero_rate": self.allow_zero_rate,
				}
			)
			item.db_set("repost_item_valuation", repost.name)
		frappe.enqueue(
			post_reposts,
			batch=self.name,
			queue="long",
			timeout=21600,
			job_id=f"stock_revaluation_batch::{self.name}",
		)


def post_reposts(batch):
	"""Run the batch's Repost Item Valuation entries in row order, whatever the automatic posting setting says."""
	names = frappe.get_all(
		"Stock Revaluation Batch Item", {"parent": batch}, pluck="repost_item_valuation", order_by="idx"
	)
	for name in names:
		execute_reposting_entry(name)
