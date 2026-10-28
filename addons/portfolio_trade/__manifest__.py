{
    "name": "Portfolio Trade / 进销存作品集",
    "summary": "Company sales approval policy and immutable quotation review history",
    "version": "18.0.1.1.0",
    "category": "Sales/Sales",
    "author": "Fu Xiaoda",
    "license": "LGPL-3",
    "depends": ["sale_management", "purchase_stock", "sale_stock", "account"],
    "data": [
        "security/portfolio_security.xml", "security/ir.model.access.csv",
        "views/portfolio_menu.xml", "views/sale_approval_views.xml",
    ],
    "application": True,
    "installable": True,
}
