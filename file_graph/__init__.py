"""
file_graph -- graph-based file organizer (BETA 0.3.44, checkpoint 4).

The fourth and biggest piece of the design discussion that also produced
checkpoints 2 (search rewrite) and 3 (dictation + OCR fallback) -- see
STATUS.md's checkpoint-4 entry for the full writeup, and each module's
own docstring for how the pieces fit together:

    metadata.py   -- cheap, no-LLM per-file evidence extraction
    scoring.py    -- evidence -> explainable 0-100% confidence, banded
                     per the design doc (>90 auto / 60-90 ask / <60 skip)
    store.py      -- Kùzu-backed persisted weights + decision log (the
                     "learns over time" half)
    organizer.py  -- ties it together into the actual organize() action,
                     the only place that ever moves a real file

No LLM anywhere in this package.
"""
