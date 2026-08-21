# Tooltip Cleanup v3.1

Every dashboard tooltip now follows one visual structure:

SOURCE
<provider / provenance>

DEFINITION
<what the metric or control means>

HOW TO INTERPRET
<how to use it in the MktScan decision process>

Implementation details:
- Summary-card hover tooltips preserve line breaks instead of collapsing content into bullets.
- `st.metric` help uses the same formatter.
- Every dataframe/table column help string is normalized automatically, including legacy one-line help copy.
- The Key Events major-event toggle also uses the same three-section format.
- Legacy tooltip copy is automatically upgraded at render time.

No database migration is required.
