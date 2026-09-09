---
type: regex
target: trace
match: contains
weight: 2
---
sessionator\s+search\b(?=[^\n]*--since\s+20\d{2}-06-05)(?=[^\n]*--until\s+20\d{2}-06-05)
