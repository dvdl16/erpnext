// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Stock Revaluation Batch", {
	onload(frm) {
		if (!frm.is_new()) return;
		if (!frm.doc.company) frm.set_value("company", frappe.defaults.get_user_default("Company"));
		if (!frm.doc.upto_posting_date) {
			frm.set_value("upto_posting_date", frappe.datetime.add_days(frappe.datetime.month_start(), -1));
		}
	},

	refresh(frm) {
		if (frm.doc.docstatus !== 0) return;
		for (const [label, method] of [
			[__("Fetch Items"), "fetch_items"],
			[__("Preview Revaluation"), "preview_revaluation"],
		]) {
			frm.add_custom_button(label, () =>
				frm.call({ method, doc: frm.doc, freeze: true }).then(() => frm.dirty())
			);
		}
	},
});
