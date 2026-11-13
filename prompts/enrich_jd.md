Extract structured information from this job posting. Return JSON only — no explanation, no markdown.

{{JOB_CONTEXT}}

Return this exact JSON schema (use null for unknown values):
{
  "visa_sponsorship": "yes" | "no" | "unknown",
  "relocation_support": "yes" | "no" | "unknown",
  "seniority": "junior" | "mid" | "senior" | "lead" | "staff" | "principal" | "unknown",
  "years_required_min": null or integer (minimum years of experience stated),
  "required_skills": ["skill1", "skill2"],
  "nice_to_have_skills": ["skill1"],
  "language_requirements": ["English"] (list all required human languages),
  "employment_type": "full_time" | "contract" | "part_time" | "unknown"
}

Rules:
- visa_sponsorship: "yes" if the posting mentions visa sponsorship, relocation, Blue Card, work permit support. "no" if it says "must have right to work", "no sponsorship", "citizens only". Otherwise "unknown".
- relocation_support: "yes" if relocation package/assistance mentioned. "no" if stated unavailable. Otherwise "unknown".
- required_skills: normalised lowercase skill names (e.g. "dbt", "snowflake", "python"). Max 20.
- language_requirements: include "English" only if explicitly required; always include non-English languages if required.
- years_required_min: the MINIMUM years stated (e.g. "3-5 years" → 3). null if not stated.
