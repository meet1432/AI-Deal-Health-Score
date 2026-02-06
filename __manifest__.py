{
    "name": "AI Deal Health Score",
    "version": "16.0.1.0.0",
    "summary": "Scores CRM opportunities based on activity, stage velocity, and sentiment",
    "category": "Sales/CRM",
    "author": "meet1432",
    "license": "LGPL-3",
    "depends": ["crm", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/res_config_settings_view.xml",
        "views/crm_lead_view.xml",
    ],
    "images": ["static/description/Icon.png"],
    "installable": True,
    "application": False,
}
