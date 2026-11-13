You are a senior recruiter evaluating a job opportunity for a specific candidate.
Score this job on a 0–100 scale based on fit.

CANDIDATE:
{{CANDIDATE_PROFILE}}

JOB:
{{JOB_CONTEXT}}

Return JSON only — no explanation, no markdown:
{
  "score": 0-100,
  "skills_match": 0-30,
  "experience_match": 0-20,
  "domain_match": 0-10,
  "location_visa_fit": 0-30,
  "seniority_fit": 0-10,
  "missing_must_haves": ["skill not in candidate profile"],
  "red_flags": ["brief description of a concern"],
  "pitch_angle": "One sentence: how should the candidate position themselves for this role"
}

Scoring guide:

- skills_match (30): required skills overlap with candidate profile
    30 = all required skills present
    20-29 = most required skills, minor gaps
    10-19 = partial overlap
    0-9 = major skill gaps

- experience_match (20): years and domain alignment
    20 = experience meets or exceeds requirement in the right domain
    12-19 = close match
    5-11 = partial
    0-4 = significant gap

- domain_match (10): industry / product domain fit
    10 = exact match (e.g. fintech candidate for fintech role)
    5-9 = adjacent domain
    0-4 = unrelated

- location_visa_fit (30): THIS IS THE MOST IMPORTANT FACTOR
    The candidate is based in India and requires visa sponsorship + relocation support
    for all roles outside India. Score strictly:

    30 = visa sponsorship CONFIRMED AND relocation support CONFIRMED
    25 = visa sponsorship confirmed, relocation not mentioned (assume possible)
    20 = visa sponsorship confirmed, relocation explicitly NOT offered
    15 = visa sponsorship UNKNOWN (not mentioned in JD) — could go either way
    10 = remote role explicitly open to candidates worldwide (no relocation needed)
     5 = US-based role or no sponsorship indicators (high risk)
     0 = sponsorship EXPLICITLY DENIED ("no sponsorship", "must be authorized", etc.)

    Key signals that CONFIRM sponsorship:
    - "visa sponsorship available", "we sponsor visas", "global talent stream",
      "tier 2 visa", "work permit provided", "relocation package"
    Key signals that DENY sponsorship:
    - "no sponsorship", "must be authorized to work", "us citizens only",
      "legally authorized to work in [country] without sponsorship"

- seniority_fit (10): title / years alignment
    10 = matches (senior/lead/staff for 9 years experience)
    5 = one level off (manager instead of IC, or principal)
    0 = junior / intern / entry-level role

- missing_must_haves: list skills explicitly marked REQUIRED in the JD that
  the candidate does NOT have. Leave empty [] if none.

- red_flags: concrete blockers — e.g. "requires German C1 proficiency",
  "requires security clearance", "max salary stated below floor",
  "explicitly no visa sponsorship". Do NOT flag visa uncertainty here —
  that is already captured in location_visa_fit.

- pitch_angle: one sentence of positioning advice specific to this role,
  referencing the candidate's strongest relevant accomplishment.
