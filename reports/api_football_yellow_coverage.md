# API-Football yellow league coverage

Generated at: `2026-06-26T15:04:12Z`
Request count: `14`

Discovery first calls `/leagues?country=<country>` without a season filter, then falls back to configured candidate league names.
Fixture statistics is `Unknown` when no league was discovered, not `No`.
Usable for StatMaker enrichment means `discovery_status == FOUND` and `coverage.fixtures.statistics == true`.

| Country | Football-Data code | Requested season | Selected API season | API-Football League ID | API-Football League Name | Available seasons | Discovery status | Fixture statistics | Events | Lineups | Players statistics | Standings | Odds | Usable for StatMaker enrichment | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Argentina | ARG | 2026 | 2026 | 128 | Liga Profesional Argentina | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015 | FOUND | Yes | Yes | Yes | Yes | Yes | No | Yes | fixture statistics available |
| Argentina | ARG | 2026 | 2026 | 129 | Primera Nacional | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011 | FOUND | No | Yes | Yes | No | Yes | Yes | No | fixture statistics not advertised for selected season |
| Austria | AUT | 2025 | 2025 | 219 | 2. Liga | 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011 | FOUND | Yes | Yes | Yes | Yes | Yes | No | Yes | fixture statistics available |
| Austria | AUT | 2025 | 2025 | 218 | Bundesliga | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011 | FOUND | Yes | Yes | Yes | Yes | Yes | No | Yes | fixture statistics available |
| Brazil | BRA | 2026 | 2026 | 1098 | Paulista Série B | 2026, 2025, 2024 | FOUND | No | Yes | Yes | No | Yes | Yes | No | fixture statistics not advertised for selected season |
| Brazil | BRA | 2026 | 2026 | 71 | Serie A | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011, 2010 | FOUND | Yes | Yes | Yes | Yes | Yes | Yes | Yes | fixture statistics available |
| Brazil | BRA | 2026 | 2026 | 72 | Serie B | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012 | FOUND | Yes | Yes | Yes | Yes | Yes | Yes | Yes | fixture statistics available |
| Denmark | DNK | 2025 | 2025 | 119 | Superliga | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011 | FOUND | Yes | Yes | Yes | Yes | Yes | No | Yes | fixture statistics available |
| Finland | FIN | 2026 | 2026 | 244 | Veikkausliiga | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012 | FOUND | Yes | Yes | Yes | Yes | Yes | Yes | Yes | fixture statistics available |
| Finland | FIN | 2026 | 2026 | 245 | Ykkönen | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012 | FOUND | No | Yes | Yes | No | Yes | Yes | No | fixture statistics not advertised for selected season |
| Ireland | IRL | 2026 | 2026 | 358 | First Division | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019 | FOUND | No | Yes | Yes | No | Yes | Yes | No | fixture statistics not advertised for selected season |
| Ireland | IRL | 2026 | 2026 | 357 | Premier Division | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019 | FOUND | Yes | Yes | Yes | Yes | Yes | Yes | Yes | fixture statistics available |
| Japan | JPN | 2026 | 2026 | 98 | J1 League | 2027, 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012 | FOUND | Yes | Yes | Yes | Yes | Yes | No | Yes | fixture statistics available |
| Mexico | MEX | 2025 | Unknown | Unknown |  |  | NOT_FOUND | Unknown | Unknown | Unknown | Unknown | Unknown | Unknown | No | No league discovered, not a stats coverage failure |
| Norway | NOR | 2026 | 2026 | 104 | 1. Division | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016 | FOUND | No | Yes | Yes | No | Yes | Yes | No | fixture statistics not advertised for selected season |
| Norway | NOR | 2026 | 2026 | 915 | 1. Division Women | 2026, 2025, 2024, 2023, 2022 | FOUND | No | Yes | Yes | No | Yes | Yes | No | fixture statistics not advertised for selected season |
| Norway | NOR | 2026 | 2026 | 103 | Eliteserien | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016 | FOUND | Yes | Yes | Yes | Yes | Yes | No | Yes | fixture statistics available |
| Poland | POL | 2025 | 2025 | 106 | Ekstraklasa | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018 | FOUND | Yes | Yes | Yes | Yes | Yes | No | Yes | fixture statistics available |
| Poland | POL | 2025 | 2025 | 107 | I Liga | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018 | FOUND | No | Yes | Yes | No | Yes | No | No | fixture statistics not advertised for selected season |
| Poland | POL | 2025 | 2025 | 109 | II Liga - East | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019 | FOUND | No | Yes | Yes | No | Yes | No | No | fixture statistics not advertised for selected season |
| Poland | POL | 2025 | 2025 | 780 | III Liga - Group 1 | 2025, 2024, 2023, 2022, 2021, 2020 | FOUND | No | Yes | Yes | No | Yes | No | No | fixture statistics not advertised for selected season |
| Poland | POL | 2025 | 2025 | 781 | III Liga - Group 2 | 2025, 2024, 2023, 2022, 2021, 2020 | FOUND | No | Yes | Yes | No | Yes | No | No | fixture statistics not advertised for selected season |
| Poland | POL | 2025 | 2025 | 782 | III Liga - Group 3 | 2025, 2024, 2023, 2022, 2021, 2020 | FOUND | No | Yes | Yes | No | Yes | No | No | fixture statistics not advertised for selected season |
| Poland | POL | 2025 | 2025 | 783 | III Liga - Group 4 | 2025, 2024, 2023, 2022, 2021, 2020 | FOUND | No | Yes | Yes | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 283 | Liga I | 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016 | FOUND | Yes | Yes | Yes | Yes | Yes | No | Yes | fixture statistics available |
| Romania | ROM | 2025 | 2025 | 284 | Liga II | 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016 | FOUND | No | Yes | Yes | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 1009 | Liga III - Play-offs | 2025, 2024, 2023, 2022 | FOUND | No | No | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 784 | Liga III - Serie 1 | 2025, 2024, 2023, 2022, 2020 | FOUND | No | No | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2024 | 793 | Liga III - Serie 10 | 2024, 2023, 2022, 2020 | FOUND | No | No | No | No | Yes | No | No | fixture statistics not advertised for selected season; requested season 2025 not available, used latest available season 2024 |
| Romania | ROM | 2025 | 2025 | 785 | Liga III - Serie 2 | 2025, 2024, 2023, 2022, 2020 | FOUND | No | Yes | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 786 | Liga III - Serie 3 | 2025, 2024, 2023, 2022, 2020 | FOUND | No | Yes | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 787 | Liga III - Serie 4 | 2025, 2024, 2023, 2022, 2020 | FOUND | No | Yes | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 788 | Liga III - Serie 5 | 2025, 2024, 2023, 2022, 2020 | FOUND | No | No | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 789 | Liga III - Serie 6 | 2025, 2024, 2023, 2022, 2020 | FOUND | No | No | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 790 | Liga III - Serie 7 | 2025, 2024, 2023, 2022, 2020 | FOUND | No | No | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2025 | 791 | Liga III - Serie 8 | 2025, 2024, 2023, 2022, 2020 | FOUND | No | Yes | No | No | Yes | No | No | fixture statistics not advertised for selected season |
| Romania | ROM | 2025 | 2024 | 792 | Liga III - Serie 9 | 2024, 2023, 2022, 2020 | FOUND | No | No | No | No | Yes | No | No | fixture statistics not advertised for selected season; requested season 2025 not available, used latest available season 2024 |
| Sweden | SWE | 2026 | 2026 | 113 | Allsvenskan | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016 | FOUND | Yes | Yes | Yes | Yes | Yes | Yes | Yes | fixture statistics available |
| Sweden | SWE | 2026 | 2026 | 549 | Damallsvenskan | 2026, 2025, 2024, 2023, 2022, 2021, 2020 | FOUND | Yes | Yes | Yes | Yes | Yes | Yes | Yes | fixture statistics available |
| Sweden | SWE | 2026 | 2026 | 114 | Superettan | 2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016 | FOUND | Yes | Yes | Yes | Yes | Yes | Yes | Yes | fixture statistics available |
