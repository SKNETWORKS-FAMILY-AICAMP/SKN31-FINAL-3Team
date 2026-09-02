// BiddingFlow RFQ portal extension. The public API method path remains the
// ERPNext default and is overridden server-side through hooks.py.

window.doc = {{ doc.as_json() }};

$(document).ready(function () {
	new RFQPortal();
	doc.supplier = "{{ doc.supplier }}";
	doc.currency = "{{ doc.currency }}";
	doc.number_format = "{{ doc.number_format }}";
	doc.buying_price_list = "{{ doc.buying_price_list }}";
});

class RFQPortal {
	constructor() {
		this.onfocus_select_all();
		this.change_qty();
		this.change_rate();
		this.change_valid_till();
		this.change_expected_delivery_date();
		this.terms();
		this.submit_rfq();
		this.navigate_quotations();
	}

	onfocus_select_all() {
		$("input").click(function () {
			$(this).select();
		});
	}

	change_qty() {
		const me = this;
		$(".rfq-items").on("change", ".rfq-qty", function () {
			me.idx = parseFloat($(this).attr("data-idx"));
			me.qty = parseFloat(flt($(this).val())) || 0;
			me.rate = parseFloat(flt($(repl(".rfq-rate[data-idx=%(idx)s]", { idx: me.idx })).val()));
			me.update_qty_rate();
			$(this).val(format_number(me.qty, doc.number_format, 2));
		});
	}

	change_rate() {
		const me = this;
		$(".rfq-items").on("change", ".rfq-rate", function () {
			me.idx = parseFloat($(this).attr("data-idx"));
			me.rate = parseFloat(flt($(this).val())) || 0;
			me.qty = parseFloat(flt($(repl(".rfq-qty[data-idx=%(idx)s]", { idx: me.idx })).val()));
			me.update_qty_rate();
			$(this).val(format_number(me.rate, doc.number_format, 2));
		});
	}

	change_valid_till() {
		$(".rfq-valid-till").on("change", function () {
			doc.valid_till = $(this).val();
		});
	}

	change_expected_delivery_date() {
		$(".rfq-items").on("change", ".rfq-expected-delivery-date", function () {
			const idx = parseInt($(this).attr("data-idx"), 10);
			const item = doc.items.find((row) => parseInt(row.idx, 10) === idx);
			if (item) {
				item.expected_delivery_date = $(this).val();
			}
		});
	}

	terms() {
		$(".terms").on("change", ".terms-feedback", function () {
			doc.terms = $(this).val();
		});
	}

	update_qty_rate() {
		doc.grand_total = 0.0;
		$.each(doc.items, (_idx, data) => {
			if (data.idx === this.idx) {
				data.qty = this.qty;
				data.rate = this.rate;
				data.amount = (this.rate * this.qty) || 0.0;
				$(repl(".rfq-amount[data-idx=%(idx)s]", { idx: this.idx })).text(
					format_number(data.amount, doc.number_format, 2)
				);
			}
			doc.grand_total += flt(data.amount);
			$(".tax-grand-total").text(format_number(doc.grand_total, doc.number_format, 2));
		});
	}

	validate_dates() {
		if (!doc.valid_till) {
			frappe.msgprint(__("Please enter Valid Till."));
			$(".rfq-valid-till").trigger("focus");
			return false;
		}

		const missing = doc.items.find((item) => !item.expected_delivery_date);
		if (missing) {
			frappe.msgprint(__("Please enter Expected Delivery Date for every item."));
			$(`.rfq-expected-delivery-date[data-idx=${missing.idx}]`).trigger("focus");
			return false;
		}
		return true;
	}

	submit_rfq() {
		const me = this;
		$(".btn-sm").click(function () {
			if (!me.validate_dates()) {
				return;
			}

			frappe.freeze();
			frappe.call({
				type: "POST",
				method: "erpnext.buying.doctype.request_for_quotation.request_for_quotation.create_supplier_quotation",
				args: { doc },
				btn: this,
				callback(r) {
					frappe.unfreeze();
					if (r.message) {
						$(".btn-sm").hide();
						window.location.href = "/supplier-quotations/" + encodeURIComponent(r.message);
					}
				},
				error() {
					frappe.unfreeze();
				},
			});
		});
	}

	navigate_quotations() {
		$(".quotations").click(function () {
			const name = $(this).attr("idx");
			window.location.href = "/supplier-quotations/" + encodeURIComponent(name);
		});
	}
}
