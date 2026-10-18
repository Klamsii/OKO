"""Traceable support for visible objects; never a legal or action verdict."""


def cash_presence_support(rows, max_gap_seconds=1.1):
    """Two nearby possible observations support presence, not origin or handling.

    Output indexes match chronological input rows. Repeated answers from the
    same image/view are not counted twice. A paper reading in a lower-detail
    full frame cannot veto repeated clear cash readings from focused hand crops.
    A focused paper reading remains a conflict. Source answers are untouched.
    """
    result=[]
    for i,row in enumerate(rows):
        cash=[x for x in row.get('objects',[]) if x.get('kind')=='cash']
        by_location={x.get('location') for x in cash}
        support=[]
        for location in sorted(by_location):
            neighbors=[]
            for j,other in enumerate(rows):
                if abs(other['seconds']-row['seconds'])>max_gap_seconds:
                    continue
                if not any(x.get('kind')=='cash' and x.get('location')==location
                           for x in other.get('objects',[])):
                    continue
                neighbors.append(j)
            paper=[x for j in neighbors for x in rows[j].get('objects',[])
                   if x.get('kind')=='paper_unknown' and x.get('location')==location]
            focused_clear=sum(any(x.get('kind')=='cash' and x.get('location')==location and
                                  x.get('certainty')=='clear' and x.get('view') in ('hands','crop')
                                  for x in rows[j].get('objects',[])) for j in neighbors)
            conflicting=bool(paper)
            hard_conflict=any(x.get('view') in ('hands','crop') for x in paper) if focused_clear else conflicting
            clear_count=sum(any(x.get('kind')=='cash' and x.get('location')==location and
                                x.get('certainty')=='clear' for x in rows[j].get('objects',[]))
                            for j in neighbors)
            tier=('strong' if len(neighbors)>=2 and clear_count>=2 and not hard_conflict else
                  'moderate' if (len(neighbors)>=2 or clear_count>=1) and not hard_conflict else
                  'tentative')
            support.append(dict(kind='cash',location=location,tier=tier,
                                observation_ids=neighbors,seconds=row['seconds'],
                                paper_ambiguity_observed=conflicting,
                                scope='visible_presence_only'))
        result.append(support)
    return result
