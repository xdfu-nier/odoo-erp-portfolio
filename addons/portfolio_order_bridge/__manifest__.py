{
    "name": "Portfolio Order Bridge",
    "summary": "Authorized external order reception, draft quotations and immutable attempts",
    "version": "18.0.1.3.0",
    "category": "Sales/Sales",
    "author": "Fu Xiaoda",
    "license": "LGPL-3",
    "depends": ["portfolio_trade"],
    "data": [
        "security/order_bridge_security.xml", "security/ir.model.access.csv",
        "views/import_source_views.xml", "views/import_job_views.xml",
        "views/import_attempt_views.xml", "wizard/import_retry_views.xml",
        "views/import_menu.xml",
    ],
    "application": False,
    "installable": True,
}
