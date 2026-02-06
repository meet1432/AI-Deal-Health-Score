from datetime import datetime, timedelta
import requests

from odoo import _, api, fields, models
from odoo.tools import html2plaintext

NEGATIVE_KEYWORDS = {
    "delay",
    "expensive",
    "later",
    "hold",
    "postpone",
    "issue",
}

RISK_THRESHOLDS = [
    (30, -40),
    (14, -25),
    (7, -10),
]

STAGE_WEIGHTS = {
    "new": -10,
    "qualified": 0,
    "proposition": 5,
    "won": 20,
}

STAGE_STALE_THRESHOLDS = {
    "new": (7, -10),
    "qualified": (14, -10),
    "proposition": (10, -10),
}


class CrmLead(models.Model):
    _inherit = "crm.lead"

    ai_health_score = fields.Integer(string="AI Health Score", default=100, tracking=True)
    ai_risk_level = fields.Selection(
        [("healthy", "Healthy"), ("warning", "Warning"), ("at_risk", "At Risk")],
        default="healthy",
        tracking=True,
    )
    ai_risk_reasons = fields.Text(string="Risk Reasons")
    ai_next_action_hint = fields.Char(string="AI Suggested Next Action")
    ai_last_scored_on = fields.Datetime(string="Last AI Scoring")

    def _get_recent_messages(self):
        self.ensure_one()
        return self.env["mail.message"].search(
            [
                ("res_id", "=", self.id),
                ("model", "=", "crm.lead"),
            ],
            order="date desc",
            limit=50,
        )

    def _compute_stage_velocity_penalty(self, reasons):
        penalty = 0
        stage_name = (self.stage_id.name or "").strip().lower()
        if stage_name in STAGE_WEIGHTS:
            weight = STAGE_WEIGHTS[stage_name]
            if weight:
                penalty += weight
                reasons.append(_("Stage weight: %s (%s)", self.stage_id.name, weight))

        if self.date_last_stage_update:
            days = (fields.Datetime.now() - self.date_last_stage_update).days
            if stage_name in STAGE_STALE_THRESHOLDS:
                threshold, value = STAGE_STALE_THRESHOLDS[stage_name]
                if days > threshold:
                    penalty += value
                    reasons.append(_("Stage '%s' stuck for %s days", self.stage_id.name, days))
            for threshold, value in RISK_THRESHOLDS:
                if days > threshold:
                    penalty += value
                    reasons.append(_("Stage unchanged for %s days", days))
                    break
        return penalty

    def _compute_engagement_penalties(self, reasons):
        penalty = 0
        activity_model = self.env["mail.activity"]
        recent_activity = activity_model.search(
            [
                ("res_model", "=", "crm.lead"),
                ("res_id", "=", self.id),
            ],
            order="write_date desc",
            limit=1,
        )
        overdue_activity = activity_model.search(
            [
                ("res_model", "=", "crm.lead"),
                ("res_id", "=", self.id),
                ("date_deadline", "<", fields.Date.today()),
            ],
            limit=1,
        )
        if overdue_activity:
            penalty += -20
            reasons.append(_("Overdue activity present"))

        last_activity_date = None
        if recent_activity:
            last_activity_date = recent_activity.write_date or recent_activity.create_date
        if not last_activity_date or (fields.Datetime.now() - last_activity_date).days > 7:
            penalty += -15
            reasons.append(_("No activity updates in 7+ days"))

        messages = self._get_recent_messages()
        last_note = next((m for m in messages if m.subtype_id and m.subtype_id.internal), None)
        if not last_note or not last_note.date or (fields.Datetime.now() - last_note.date).days > 7:
            penalty += -10
            reasons.append(_("No internal note in 7+ days"))

        last_email = next(
            (
                m
                for m in messages
                if m.message_type == "email"
                and m.author_id
                and m.author_id != self.env.user.partner_id
            ),
            None,
        )
        if not last_email or not last_email.date or (fields.Datetime.now() - last_email.date).days > 10:
            penalty += -15
            reasons.append(_("No customer email reply in 10+ days"))
        return penalty

    def _compute_sentiment_penalty(self, reasons):
        penalty = 0
        messages = self._get_recent_messages()
        combined = " ".join((html2plaintext(m.body or "") for m in messages))
        print("\n\n\n",combined)
        if not combined.strip():
            reasons.append(_("Sentiment: No recent customer chat to assess"))
            return penalty

        api_url = self.env["ir.config_parameter"].sudo().get_param(
            "ai_deal_health_score.sentiment_api_url", default="http://localhost:7860"
        )
        if api_url:
            try:
                response = requests.post(
                    f"{api_url}/sentiment",
                    json={"text": combined[:4000]},
                    timeout=30,
                )
                if response.status_code == 200:
                    data = response.json()
                    label = (data.get("label") or "").lower()
                    score = float(data.get("score") or 0.0)
                    brief_reason = data.get("brief_reason") or "Sentiment assessed by AI"

                    if label == "negative":
                        penalty = -35
                    elif label == "positive":
                        penalty = 0

                    reasons.append(_("Sentiment: %s", brief_reason))
                    return penalty
            except Exception:
                pass

        if any(k in combined.lower() for k in NEGATIVE_KEYWORDS):
            penalty = -20
            reasons.append(_("Sentiment: Negative (fallback keywords)"))
        else:
            reasons.append(_("Sentiment: Neutral (fallback keywords)"))
        return penalty

    def _derive_risk_level(self, score):
        if score >= 80:
            return "healthy"
        if score >= 50:
            return "warning"
        return "at_risk"

    def _suggest_next_action(self, reasons):
        reason_text = " ".join(reasons).lower()
        if "no customer email" in reason_text:
            return "Email"
        if "no recent activities" in reason_text:
            return "Call"
        if "stage unchanged" in reason_text:
            return "Meeting"
        return "Todo"

    def _ensure_activity(self, activity_type_name):
        activity_type = self.env["mail.activity.type"].search([("name", "=", activity_type_name)], limit=1)
        if not activity_type:
            return
        existing = self.env["mail.activity"].search(
            [
                ("res_model", "=", "crm.lead"),
                ("res_id", "=", self.id),
                ("activity_type_id", "=", activity_type.id),
                ("user_id", "=", self.user_id.id),
                ("state", "in", ["planned", "today", "overdue"]),
            ],
            limit=1,
        )
        if existing:
            return
        self.env["mail.activity"].create(
            {
                "activity_type_id": activity_type.id,
                "res_model_id": self.env["ir.model"]._get_id("crm.lead"),
                "res_id": self.id,
                "user_id": self.user_id.id or self.env.user.id,
                "date_deadline": fields.Date.today() + timedelta(days=1),
                "summary": "AI flagged deal as At-Risk — suggested follow-up",
            }
        )

    def _post_risk_chatter(self, score, reasons, action):
        body = _(
            "<p><b>🤖 AI Deal Health Alert</b></p>"
            "<p>Score: %s (%s)</p>"
            "<p>Reasons:</p><ul>%s</ul>"
            "<p>Suggested action: <b>%s</b></p>",
            score,
            self.ai_risk_level.replace("_", " ").title(),
            "".join([f"<li>{r}</li>" for r in reasons]) if reasons else "<li>No risk factors detected</li>",
            action,
        )
        self.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note")

    def action_ai_score(self):
        for lead in self:
            score = 100
            reasons = []

            score += lead._compute_stage_velocity_penalty(reasons)
            score += lead._compute_engagement_penalties(reasons)
            score += lead._compute_sentiment_penalty(reasons)

            score = max(0, min(100, score))
            previous_level = lead.ai_risk_level
            new_level = lead._derive_risk_level(score)

            reason_text = " ".join(reasons).lower()
            if "strongly negative" in reason_text:
                score = min(score, 40)
                new_level = "at_risk"
            elif "sentiment: negative" in reason_text:
                score = min(score, 55)
                if new_level == "healthy":
                    new_level = "warning"
            elif "sentiment: positive" in reason_text:
                score = min(100, score + 25)
                if "overdue activity" not in reason_text:
                    score = max(score, 80)
                    new_level = "healthy"

            lead.write(
                {
                    "ai_health_score": score,
                    "ai_risk_level": new_level,
                    "ai_risk_reasons": "\n".join(reasons),
                    "ai_next_action_hint": lead._suggest_next_action(reasons),
                    "ai_last_scored_on": fields.Datetime.now(),
                }
            )

            if previous_level != "at_risk" and new_level == "at_risk":
                action_type = lead._suggest_next_action(reasons)
                lead._ensure_activity(action_type)
                lead._post_risk_chatter(score, reasons, action_type)

        return True

    @api.model
    def cron_ai_score(self):
        leads = self.search([("type", "=", "opportunity"), ("active", "=", True)])
        for batch in [leads[i : i + 200] for i in range(0, len(leads), 200)]:
            batch.action_ai_score()
