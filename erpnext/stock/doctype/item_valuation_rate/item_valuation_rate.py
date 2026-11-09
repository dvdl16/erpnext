# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ItemValuationRate(Document):
	"""Rate a Periodic Average item leaves stock at from valid_from, frozen by a Stock Revaluation Batch."""
