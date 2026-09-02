app_name = "biddingflow_rfq_portal"
app_title = "BiddingFlow RFQ Portal"
app_publisher = "BiddingFlow"
app_description = "Supplier RFQ portal extensions for BiddingFlow"
app_email = "dev@biddingflow.local"
app_license = "MIT"

required_apps = ["erpnext"]

# Keep the ERPNext public method path unchanged for the browser while replacing
# only its implementation. Existing RFQ e-mail links therefore keep working.
override_whitelisted_methods = {
    "erpnext.buying.doctype.request_for_quotation.request_for_quotation.create_supplier_quotation": (
        "biddingflow_rfq_portal.overrides.rfq.create_supplier_quotation"
    )
}
