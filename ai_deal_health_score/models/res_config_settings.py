from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_sentiment_api_url = fields.Char(
        string="AI Sentiment API URL",
        config_parameter="ai_deal_health_score.sentiment_api_url",
        default="http://localhost:7861",
    )
