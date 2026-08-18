#!/usr/bin/env python3
from pathlib import Path

SOURCE = Path("app/src/main/java/com/statmaker/app/PreparedBettingSnapshotStore.kt")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"Could not locate {label}; found {count} matches")
    return text.replace(old, new, 1)


def main() -> None:
    text = SOURCE.read_text(encoding="utf-8")

    text = replace_once(
        text,
        '''        val compactCatalogFeed = catalogFeed.toPreparedCatalogFeed()
        val uniqueSelections = selections.distinctBy(::preparedSelectionKey)
        val uniqueMatches = linkedMapOf<String, OddsMatch>()
''',
        '''        val compactCatalogFeed = catalogFeed.toPreparedCatalogFeed()
        // Publisher-only optimization: materialize the immutable selection key once. The normal
        // implementation can otherwise hash the same selection repeatedly during evidence,
        // decision, persistence and cache publication.
        val uniqueSelections = selections
            .distinctBy(::preparedSelectionKey)
            .map { selection ->
                val key = preparedSelectionKey(selection)
                if (selection.preparedSelectionKey == key) selection
                else selection.copy(preparedSelectionKey = key)
            }
        val uniqueMatches = linkedMapOf<String, OddsMatch>()
''',
        "prepared key materialization",
    )

    text = replace_once(
        text,
        '''        clearBookmakerAlignedEvidenceRuntimeCache()
        val sharedEvidenceByAuditKey = sharedBettingEvidenceBatch(uniqueSelections)
        val bookmakerEvidenceBySelectionKey = uniqueSelections.mapNotNull { selection ->
''',
        '''        Log.i("StatMakerAppReady", "stage=store_${competitionId}_evidence_begin selections=${uniqueSelections.size}")
        clearBookmakerAlignedEvidenceRuntimeCache()
        val sharedEvidenceByAuditKey = sharedBettingEvidenceBatch(uniqueSelections)
        Log.i("StatMakerAppReady", "stage=store_${competitionId}_evidence_complete entries=${sharedEvidenceByAuditKey.size}")
        val bookmakerEvidenceBySelectionKey = uniqueSelections.mapNotNull { selection ->
''',
        "evidence timing markers",
    )

    text = replace_once(
        text,
        '''        val decisionsBySelectionKey = uniqueSelections.associate { selection ->
''',
        '''        Log.i("StatMakerAppReady", "stage=store_${competitionId}_decisions_begin")
        val decisionsBySelectionKey = uniqueSelections.associate { selection ->
''',
        "decision start marker",
    )

    text = replace_once(
        text,
        '''            preparedSelectionKey(selection) to decision
        }

        val db = writableDatabase
        db.beginTransaction()
''',
        '''            preparedSelectionKey(selection) to decision
        }
        Log.i("StatMakerAppReady", "stage=store_${competitionId}_decisions_complete entries=${decisionsBySelectionKey.size}")

        val db = writableDatabase
        // The publisher builds a disposable database from scratch and exports it only after a
        // successful complete build. Maintaining three large selection indexes row-by-row during
        // the 25k+ Domestic insert is pure overhead. Drop them for the bulk load and rebuild them
        // before the database is exported. The shipped DB therefore has the exact normal indexes.
        val publisherBulkLoad = uniqueSelections.size >= 5000
        if (publisherBulkLoad) {
            Log.i("StatMakerAppReady", "stage=store_${competitionId}_bulk_setup_begin")
            db.execSQL("PRAGMA synchronous=OFF")
            db.execSQL("PRAGMA temp_store=MEMORY")
            db.execSQL("PRAGMA cache_size=-65536")
            db.execSQL("DROP INDEX IF EXISTS idx_prepared_selections_scope")
            db.execSQL("DROP INDEX IF EXISTS idx_prepared_selections_pattern")
            db.execSQL("DROP INDEX IF EXISTS idx_prepared_selections_builder")
            Log.i("StatMakerAppReady", "stage=store_${competitionId}_bulk_setup_complete")
        }
        Log.i("StatMakerAppReady", "stage=store_${competitionId}_db_begin matches=${uniqueMatches.size} selections=${uniqueSelections.size}")
        db.beginTransaction()
''',
        "bulk database setup",
    )

    text = replace_once(
        text,
        '''                ) { "Could not store prepared match $matchKey" }
            }

            uniqueSelections.forEach { selection ->
''',
        '''                ) { "Could not store prepared match $matchKey" }
            }
            Log.i("StatMakerAppReady", "stage=store_${competitionId}_matches_complete count=${uniqueMatches.size}")

            uniqueSelections.forEachIndexed { selectionIndex, selection ->
''',
        "selection insert loop",
    )

    text = replace_once(
        text,
        '''                check(
                    db.insertOrThrow("prepared_selections", null, values) != -1L
                ) { "Could not store prepared selection ${preparedSelectionKey(selection)}" }
            }

            val metaValues = ContentValues().apply {
''',
        '''                check(
                    db.insertOrThrow("prepared_selections", null, values) != -1L
                ) { "Could not store prepared selection ${preparedSelectionKey(selection)}" }
                val completed = selectionIndex + 1
                if (completed % 5000 == 0 || completed == uniqueSelections.size) {
                    Log.i("StatMakerAppReady", "stage=store_${competitionId}_selection_rows completed=$completed total=${uniqueSelections.size}")
                }
            }
            Log.i("StatMakerAppReady", "stage=store_${competitionId}_selection_insert_complete")

            val metaValues = ContentValues().apply {
''',
        "selection insert progress",
    )

    text = replace_once(
        text,
        '''        } finally {
            db.endTransaction()
        }

        uniqueSelections.forEach { selection ->
''',
        '''        } finally {
            db.endTransaction()
        }
        Log.i("StatMakerAppReady", "stage=store_${competitionId}_db_commit_complete")
        if (publisherBulkLoad) {
            Log.i("StatMakerAppReady", "stage=store_${competitionId}_reindex_begin")
            createPerformanceIndexes(db)
            Log.i("StatMakerAppReady", "stage=store_${competitionId}_reindex_complete")
        }

        uniqueSelections.forEach { selection ->
''',
        "post-commit reindex",
    )

    SOURCE.write_text(text, encoding="utf-8")
    print("APP_READY_PREPARED_BULK_OK")


if __name__ == "__main__":
    main()
