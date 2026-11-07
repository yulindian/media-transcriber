"""Explicit quality preconditions for synthetic legacy export fixtures only."""
import quality
import documents


def checked_export(job, output):
    report=quality.check(job)
    quality.accept(job,{'snapshot_fingerprint':report['snapshot_fingerprint'],
                       'accepted_issues':[{'turn_id':t['turn_id'],'reason':'Synthetic fixture: expected diagnostic accepted by this test'} for t in report['issues']]})
    return documents.finalize(job,output)
