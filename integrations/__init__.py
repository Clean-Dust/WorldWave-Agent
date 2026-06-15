"""ww/integrations — Platform integrations and devops tooling.

Separated from core/ (2026-06-16) to decouple CI/CD automation
from cognitive logic, per Gemini architectural review.
"""

from integrations.github_pr_bot import PRBot, PRInfo, ReviewResult

__all__ = ["PRBot", "PRInfo", "ReviewResult"]
