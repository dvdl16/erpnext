// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Stock Revaluation Batch", {
	onload(frm) {
		if (!frm.is_new() || frm.doc.from_date) return;
		const month_start = frappe.datetime.month_start();
		frm.set_value("from_date", frappe.datetime.add_months(month_start, -1));
		frm.set_value("to_date", frappe.datetime.add_days(month_start, -1));
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
