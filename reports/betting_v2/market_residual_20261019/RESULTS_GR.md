# Πρόβλεψη πλεονεκτήματος έναντι της αγοράς

Σταθερά prior-only μοντέλα ανά metric, ίδιοι αντίπαλοι και ίδιοι περιορισμοί. Ανάπτυξη, όχι ανεξάρτητο holdout.

| Σκέλος | Πρώτη: bets / ROI | Τελευταία: bets / ROI |
|---|---:|---:|
| stat_point | 424 / +7.09% | 451 / +8.37% |
| stat_conservative | 191 / +3.97% | 178 / +11.64% |
| market_point | 0 / — | 1 / -100.00% |
| market_conservative | 0 / — | 0 / — |
| residual_point | 80 / +24.02% | 51 / +13.46% |
| residual_conservative | 35 / +34.01% | 24 / +17.35% |

Το market benchmark χρησιμοποιεί την ίδια model push mass· δεν ισχυρίζεται ότι η αγορά δίνει γνωστή πιθανότητα push. Οι decisions κρατούν όλους τους αρχικούς στατιστικούς/favourite περιορισμούς και μόνο μειώνουν την raw πιθανότητα. Πρώτα point winner, μετά conservative abstention χωρίς replacement. Κανένα ROI tuning ή αυτόματη προαγωγή.
