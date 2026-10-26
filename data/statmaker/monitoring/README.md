# Κατανάλωση API-Football

Το [CSV](api_football_usage.csv) δείχνει ανά ημέρα την τελευταία παρατηρημένη κατανάλωση,
το ημερήσιο όριο, το υπόλοιπο, το ποσοστό χρήσης και την ώρα μέτρησης.
Ανοίγει στο GitHub ως πίνακας και μπορεί να αποθηκευτεί από το Raw για χρήση στο Excel.

Η ημέρα ακολουθεί UTC, όπως ο μετρητής του παρόχου. Κρατάμε εφεδρεία 1.500 αιτημάτων·
η στήλη `availableAboveReserve` δείχνει πόσα απομένουν πάνω από αυτήν.

Οι νέες μετρήσεις προέρχονται από το δωρεάν `/status`. Η καταγραφή γίνεται μαζί με
το Live Settlement Watchdog. Οι αγωνιστικές ενημερώσεις έχουν ξεχωριστή προτεραιότητα και κοινό ημερήσιο όριο.
Αποθηκεύονται μόνο μετρητές, ώρα και σύνδεσμος εκτέλεσης, χωρίς κλειδί ή στοιχεία λογαριασμού.

Οι παλιές γραμμές με `source=workflow-log` ανακτήθηκαν από headers στα logs.
Η `observedAtUtc` είναι η ώρα της συγκεκριμένης μέτρησης. Δεν αποτελούν εγγυημένα
τελικά σύνολα 24ώρου. Ημέρες χωρίς παρατήρηση δεν συμπληρώνονται με μηδενικά.

## Κοινός ημερήσιος κόφτης

Όλες οι τρέχουσες εργασίες API-Football μοιράζονται ατομικές δεσμεύσεις κλήσεων,
ακόμη και όταν εκτελούνται παράλληλα. Ιστορικά δεδομένα, backfills και χαμηλή
προτεραιότητα σταματούν στις 4.500. Όλες οι εργασίες σταματούν στις 6.000 ή
νωρίτερα αν χρειάζεται για να μείνουν τουλάχιστον 1.500 στο πλάνο.
Αν ο μετρητής ή το κοινό αρχείο δεν διαβάζεται, νέες πληρωτέες κλήσεις δεν γίνονται.

Η [κοινή κατανομή](https://github.com/Velliouras/StatMaker-Data/blob/api-football-budget/data/statmaker/monitoring/api_football_budget_state.json)
αποθηκεύεται σε ξεχωριστό branch. Το `estimatedAllocated` περιλαμβάνει και
δεσμευμένες κλήσεις που μπορεί να μην έχουν χρησιμοποιηθεί: δεν είναι μετρητής χρέωσης.
Δεσμεύσεις από τερματισμένες εκτελέσεις δεν απελευθερώνονται αυθαίρετα· μετρούν
συντηρητικά μέχρι την επόμενη ημέρα UTC.

## Ενημέρωση αποτελεσμάτων και επίσημων βαθμολογιών

| Προτεραιότητα | Αποτελέσματα | Βαθμολογίες σε ενεργή αγωνιστική |
| --- | --- | --- |
| Αγγλία Premier League, Ισπανία La Liga, Γερμανία Bundesliga, Ιταλία Serie A, Γαλλία Ligue 1, Ελλάδα Super League και UEFA | 15 λεπτά | 30 λεπτά |
| Υπόλοιπη Ευρώπη | 30 λεπτά | 60 λεπτά |
| Υπόλοιπος κόσμος | 60 λεπτά | 120 λεπτά |

Χωρίς κοντινούς αγώνες, οι βαθμολογίες ελέγχονται καθημερινά. Οι χρονικές ζώνες
ελέγχου είναι κοινές για cron και watchdog, ώστε δεύτερο ξύπνημα στο ίδιο διάστημα
να μην ξοδεύει ξανά κλήσεις. Το GitHub μπορεί να καθυστερήσει εκτελέσεις και ο
πάροχος μπορεί να ενημερώσει αργότερα την επίσημη βαθμολογία.

Το [αρχείο ενημέρωσης](../priority_refresh/index.json) περιλαμβάνει κλήσεις ανά
εκτέλεση, παύσεις λόγω ορίου και σφάλματα. Κάθε λίγκα έχει ξεχωριστά
`resultsCheckedAtUTC`, `standingsCheckedAtUTC`, `standingsObservedAtUTC` και πλήρη σεζόν.
Αποτελέσματα και βαθμολογίες δημοσιεύονται χωρίς Android emulator ή Actions artifacts.
Ο κόφτης ελέγχει αυτές τις εργασίες· χρήση του ίδιου κλειδιού από άλλα συστήματα
πρέπει να συνυπολογίζεται ξεχωριστά.

### Εκκίνηση και καθυστερήσεις GitHub

Οι χρόνοι 15/30/60/120 λεπτών είναι η συχνότητα ελέγχου των δεδομένων όταν
εκτελείται ο παραγωγός. Το GitHub cron δεν εγγυάται ακριβή εκκίνηση: μπορεί να
καθυστερήσει ή να παραλείψει γεγονότα. Πλέον ζητάμε αφύπνιση κάθε 5 λεπτά και
επιπλέον αφυπνίσεις από ολοκλήρωση ανεξάρτητων workflows δεδομένων ή του ελέγχου
του μηχανισμού. Μια δεύτερη εκκίνηση στο ίδιο χρονικό διάστημα δεν καλεί ξανά το API.
Ο watchdog εξακολουθεί να καλεί τον ίδιο παραγωγό απευθείας.

Τα `resultsCheckedAtUTC` και `standingsObservedAtUTC` δείχνουν πραγματική
επιτυχημένη ανάκτηση, όχι προγραμματισμένο χρόνο. Ένα επιτυχές χειροκίνητο run
δεν αποδεικνύει ότι λειτουργεί το cron. Για αυστηρή χρονική εγγύηση απαιτείται
χρονιστής εκτός του GitHub scheduler.

### Scorers on the external Watchdog

The existing 15-minute external dispatch also invokes domestic match scorers,
the six existing domestic top-scorer leaderboards, and UEFA league-phase scorers.
No additional cron-job.org job or Android build is needed.

- Domestic scorer timelines consume the verified priority-results feed, so new
  finals do not wait for the nightly enriched archive. Recent gaps are checked
  once per UTC half-hour, with at most 32 provider calls per needed run.
- Leaderboards retain their normal 20-hour cache on quiet days. A verified final
  in the last six hours shortens the cache to 30 minutes (at most six calls).
- UEFA retries missing verified league-phase goal events with a 30-minute gate
  and a 64-call run ceiling. Qualifiers before the phase start are excluded.
- Complete score-reconciled event caches are reused. Published history retains
  the whole selected season; only new network work has a 14-day domestic window.
- All three producers retain the common 6,000-call daily stop, 4,500 background
  stop and 1,500-call provider reserve. Exhaustion preserves verified caches.

The six domestic scorer lists are also reconciled against complete verified
team-season goal timelines. This zero-request step runs even when the provider
top-20 cache is fresh, and adds omitted players by numeric provider player ID.
Own goals are excluded; both sides' event counts must match the final score.
Unknown assists remain null. New season totals require complete team coverage.
