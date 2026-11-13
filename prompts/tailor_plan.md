You are building a targeted resume for a specific job. Select the best bullets from the candidate's profile.

CANDIDATE PROFILE:
{{PROFILE_SUMMARY}}

JOB:
{{JD_CONTEXT}}

Return JSON only — no explanation, no markdown.

{
  "archetype": "ANALYTICS_ENGINEER_SAAS" | "FINTECH_RISK" | "DATA_PLATFORM_ML" | "BIG_COMPANY_LEAD",
  "title_variant": "AE" | "FIN" | "PLAT" | "BIGCO" | "DEFAULT",
  "summary_variant": "AE" | "FIN" | "PLAT" | "BIGCO" | "DEFAULT",
  "bullet_selections": [
    {
      "bullet_id": "VISTA-01",
      "use_alt": false,
      "rewrite_hint": "Mirror 'pipeline optimisation' from the JD"
    }
  ],
  "skills_first": ["dbt", "snowflake", "python"],
  "positioning_note": "One sentence: how to position this candidate for this specific role"
}

Archetype guide:
- ANALYTICS_ENGINEER_SAAS: role focuses on dbt, data modelling, metrics, analyst self-service, BI tools, SaaS company
- FINTECH_RISK: banking, insurance, fintech, risk, compliance, regulatory, credit data
- DATA_PLATFORM_ML: distributed systems, platform engineering, ML infrastructure, Spark, Databricks, scale
- BIG_COMPANY_LEAD: large tech/enterprise, lead/staff/principal, mentoring, design reviews, standards

Bullet selection rules:
- Select 7–10 bullets total across all jobs
- Always include at least 2 Vista bullets
- For BIGCO: lead bullet (VISTA-07) must be first
- For AE: semantic layer bullet (VISTA-02) must be first
- Prefer bullets whose tags match the archetype (AE→AE tag, PLAT→PLAT tag, FIN→FIN tag, LEAD→LEAD tag)
- If use_alt=true, the ALT variant of the bullet is used (only valid if the bullet has an enabled ALT)
- rewrite_hint: specific instruction for the rewriter (keep empty if no change needed)
