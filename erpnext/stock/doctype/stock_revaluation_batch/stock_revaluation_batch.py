# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import flt, get_datetime, getdate

import erpnext
from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.accounts.utils import get_fiscal_year
from erpnext.controllers.stock_controller import future_sle_exists
from erpnext.stock import get_warehouse_account_map
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import execute_reposting_entry
from erpnext.stock.stock_ledger import (
	get_frozen_valuation_rate,
	get_items_to_be_repost,
	make_sl_entries,
	update_entries_after,
)
from erpnext.stock.utils import is_periodic_average

TRANSFER_PURPOSES = ("Material Transfer", "Material Transfer for Manufacture", "Send to Subcontractor")
PERIOD_END_TIME = "23:59:59"


class StockRevaluationBatch(Document):
	"""Month end valuation run. Posts the reposts held back for Periodic Average items, then
	prices each item's period issues and closing stock at the period's weighted average and
	freezes that rate for the next period."""

	def validate(self):
		if getdate(self.from_date) > getdate(self.upto_posting_date):
			frappe.throw(_("Period Start must be on or before Period End"))
		self.posting_date = self.upto_posting_date
		self.posting_time = PERIOD_END_TIME
		if not self.items and not self.rates:
			self.fetch_items()
		self.set_totals()

	@frappe.whitelist()
	def fetch_items(self):
		self.fetch_reposts()
		self.fetch_rates()
		self.set_totals()

	def fetch_reposts(self):
		"""One row per repost queued for the company up to the period end."""
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

	def fetch_rates(self):
		"""One row per Periodic Average item with stock or receipts in the period."""
		self.rates = []
		for item_code in self.get_pooled_items():
			if valuation := self.get_period_valuation(item_code):
				self.append("rates", valuation)

	def get_pooled_items(self):
		items = frappe.get_all(
			"Stock Ledger Entry",
			filters={
				"company": self.company,
				"is_cancelled": 0,
				"posting_date": ("<=", self.upto_posting_date),
			},
			pluck="item_code",
			distinct=True,
			order_by="item_code",
		)
		return [item for item in items if is_periodic_average(item, self.company)]

	def get_period_valuation(self, item_code):
		"""Dynamics style period average: opening stock at the frozen rate plus the period's receipts."""
		current_rate = flt(get_frozen_valuation_rate(item_code, self.company, get_datetime(self.from_date)))
		opening_qty = flt(
			self.get_ledger_sum(item_code, "actual_qty", upto=getdate(self.from_date), inclusive=False)
		)
		receipt_qty, receipt_value = self.get_period_movement(item_code, inward=True)
		pool_qty = opening_qty + receipt_qty
		if pool_qty <= 0:
			return None

		period_rate = (opening_qty * current_rate + receipt_value) / pool_qty
		issue_qty = -self.get_period_movement(item_code, inward=False)[0]
		closing_qty = flt(self.get_ledger_sum(item_code, "actual_qty"))
		closing_value = flt(self.get_ledger_sum(item_code, "stock_value_difference"))
		return {
			"item_code": item_code,
			"current_rate": current_rate,
			"opening_qty": opening_qty,
			"opening_value": opening_qty * current_rate,
			"receipt_qty": receipt_qty,
			"receipt_value": receipt_value,
			"period_rate": period_rate,
			"issue_qty": issue_qty,
			"closing_qty": closing_qty,
			"cogs_adjustment": issue_qty * (period_rate - current_rate),
			"stock_adjustment": closing_qty * period_rate - closing_value,
		}

	def get_ledger_sum(self, item_code, field, upto=None, inclusive=True):
		sle = frappe.qb.DocType("Stock Ledger Entry")
		upto = upto or getdate(self.upto_posting_date)
		date_condition = sle.posting_date <= upto if inclusive else sle.posting_date < upto
		return (
			frappe.qb.from_(sle)
			.select(Sum(sle[field]))
			.where(
				(sle.item_code == item_code)
				& (sle.company == self.company)
				& (sle.is_cancelled == 0)
				& date_condition
			)
			.run()[0][0]
		)

	def get_period_movement(self, item_code, inward):
		"""Qty and value received (or issued) in the period, transfers between warehouses left out."""
		sle, stock_entry = frappe.qb.DocType("Stock Ledger Entry"), frappe.qb.DocType("Stock Entry")
		qty, value = (
			frappe.qb.from_(sle)
			.left_join(stock_entry)
			.on((sle.voucher_type == "Stock Entry") & (sle.voucher_no == stock_entry.name))
			.select(Sum(sle.actual_qty), Sum(sle.stock_value_difference))
			.where(
				(sle.item_code == item_code)
				& (sle.company == self.company)
				& (sle.is_cancelled == 0)
				& (sle.posting_date >= getdate(self.from_date))
				& (sle.posting_date <= getdate(self.upto_posting_date))
				& (sle.actual_qty > 0 if inward else sle.actual_qty < 0)
				& (stock_entry.purpose.isnull() | stock_entry.purpose.notin(TRANSFER_PURPOSES))
			)
			.run()[0]
		)
		return flt(qty), flt(value)

	@frappe.whitelist()
	def preview_revaluation(self):
		"""Run every queued repost inside a savepoint, record the resulting values, then roll back.

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
		self.total_cogs_adjustment = sum(flt(row.cogs_adjustment) for row in self.rates)
		self.total_stock_adjustment = sum(flt(row.stock_adjustment) for row in self.rates)

	def on_submit(self):
		if not self.items and not self.rates:
			frappe.throw(_("Nothing to post"))
		frappe.enqueue(
			post_batch,
			batch=self.name,
			queue="long",
			timeout=21600,
			job_id=f"stock_revaluation_batch::{self.name}",
			now=frappe.in_test,
		)

	def post_period_valuation(self):
		if not self.rates:
			return
		self.freeze_rates()
		self.repost_period()
		make_sl_entries(self.get_revaluation_entries())
		self.make_gl_entries()
		self.repost_after_period_end()

	def freeze_rates(self):
		for row in self.rates:
			frappe.get_doc(
				{
					"doctype": "Item Valuation Rate",
					"item_code": row.item_code,
					"company": self.company,
					"valid_from": self.from_date,
					"valuation_rate": row.period_rate,
					"stock_revaluation_batch": self.name,
				}
			).insert(ignore_permissions=True)

	def repost_period(self):
		"""Reprice the period's issues at the frozen rates, warehouse by warehouse, GL included."""
		pairs = frappe.get_all(
			"Stock Ledger Entry",
			filters={
				"company": self.company,
				"item_code": ("in", [row.item_code for row in self.rates]),
				"posting_date": (">=", self.from_date),
				"is_cancelled": 0,
			},
			fields=["item_code", "warehouse"],
			distinct=True,
		)
		for pair in pairs:
			self.execute_repost(
				{
					"based_on": "Item and Warehouse",
					"item_code": pair.item_code,
					"warehouse": pair.warehouse,
					"posting_date": self.from_date,
					"posting_time": "00:00:00",
				}
			)

	def execute_repost(self, args):
		"""Run a repost now, whatever Disable Automatic Revaluation Posting says."""
		repost = frappe.get_doc({"doctype": "Repost Item Valuation", "company": self.company, **args})
		repost.flags.ignore_permissions = True
		repost.flags.dont_run_in_test = True
		repost.submit()
		execute_reposting_entry(repost.name)

	def get_revaluation_entries(self):
		"""A zero qty entry per warehouse holding a repriced item, moving it onto the period rate."""
		sle = frappe.qb.DocType("Stock Ledger Entry")
		entries = []
		for row in self.rates:
			balances = (
				frappe.qb.from_(sle)
				.select(sle.warehouse, Sum(sle.actual_qty).as_("qty"))
				.where(
					(sle.item_code == row.item_code)
					& (sle.company == self.company)
					& (sle.is_cancelled == 0)
					& (sle.posting_date <= getdate(self.upto_posting_date))
				)
				.groupby(sle.warehouse)
			).run(as_dict=True)
			entries.extend(
				self.get_sl_entry(row, balance.warehouse) for balance in balances if flt(balance.qty)
			)
		return entries

	def get_sl_entry(self, row, warehouse):
		return frappe._dict(
			{
				"item_code": row.item_code,
				"warehouse": warehouse,
				"posting_date": self.posting_date,
				"posting_time": self.posting_time,
				"fiscal_year": get_fiscal_year(self.posting_date, company=self.company)[0],
				"voucher_type": self.doctype,
				"voucher_no": self.name,
				"voucher_detail_no": row.name,
				"actual_qty": 0,
				"incoming_rate": 0,
				"is_revaluation": 1,
				"stock_uom": frappe.get_cached_value("Item", row.item_code, "stock_uom"),
				"company": self.company,
				"is_cancelled": 0,
			}
		)

	def get_inventory_account_map(self):
		return get_warehouse_account_map(self.company)

	def get_gl_entries(self, inventory_account_map=None):
		"""Each revaluation entry moves its warehouse account against the Stock Adjustment account."""
		inventory_account_map = inventory_account_map or self.get_inventory_account_map()
		adjustment_account, cost_center, currency = frappe.get_cached_value(
			"Company", self.company, ["stock_adjustment_account", "cost_center", "default_currency"]
		)
		if not adjustment_account:
			frappe.throw(_("Please set Stock Adjustment Account in Company {0}").format(self.company))

		gl_entries = []
		entries = frappe.get_all(
			"Stock Ledger Entry",
			{"voucher_type": self.doctype, "voucher_no": self.name, "is_cancelled": 0},
			["warehouse", "stock_value_difference"],
		)
		for entry in entries:
			account = inventory_account_map.get(entry.warehouse, {}).get("account")
			if not account:
				frappe.throw(_("Warehouse {0} is not linked to any account").format(entry.warehouse))
			if flt(entry.stock_value_difference):
				gl_entries.append(
					self.get_gl_dict(
						account, adjustment_account, entry.stock_value_difference, cost_center, currency
					)
				)
				gl_entries.append(
					self.get_gl_dict(
						adjustment_account, account, -entry.stock_value_difference, cost_center, currency
					)
				)
		return gl_entries

	def get_gl_dict(self, account, against, debit, cost_center, currency):
		return frappe._dict(
			{
				"account": account,
				"against": against,
				"debit": debit,
				"credit": 0,
				"debit_in_account_currency": debit,
				"credit_in_account_currency": 0,
				"account_currency": currency,
				"cost_center": cost_center,
				"posting_date": self.posting_date,
				"voucher_type": self.doctype,
				"voucher_no": self.name,
				"company": self.company,
				"remarks": _("Stock revalued to the period average"),
				"is_opening": "No",
			}
		)

	def make_gl_entries(self, gl_entries=None, from_repost=False):
		if not erpnext.is_perpetual_inventory_enabled(self.company):
			return
		make_gl_entries(gl_entries or self.get_gl_entries(), from_repost=from_repost)

	def repost_after_period_end(self):
		"""Entries already posted after the period take the new rate through an ordinary repost."""
		args = frappe._dict(
			{
				"voucher_type": self.doctype,
				"voucher_no": self.name,
				"posting_date": self.posting_date,
				"posting_time": self.posting_time,
				"company": self.company,
			}
		)
		if future_sle_exists(args, for_update=True):
			self.execute_repost({"based_on": "Transaction", **args})


def get_repost_targets(repost):
	"""Item and warehouse pairs a repost recalculates, each from its own posting datetime."""
	if repost.based_on == "Item and Warehouse":
		return [repost]
	return [frappe._dict(row) for row in get_items_to_be_repost(repost.voucher_type, repost.voucher_no)]


def post_batch(batch):
	"""Queued reposts first, in row order, then the period valuation on the repaired ledger."""
	doc = frappe.get_doc("Stock Revaluation Batch", batch)
	for item in doc.items:
		execute_reposting_entry(item.repost_item_valuation)
	doc.post_period_valuation()
