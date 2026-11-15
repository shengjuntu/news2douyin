"""Code-owned GDELT 2.0 source catalog; raw retention never uses topic filters."""
FEEDS = {
    'gkg_english': dict(label='GDELT GKG English', kind='gkg', manifest='lastupdate.txt', suffix='.gkg.csv.zip'),
    'gkg_translation': dict(label='GDELT GKG Translingual', kind='gkg', manifest='lastupdate-translation.txt', suffix='.translation.gkg.csv.zip'),
    'events_english': dict(label='GDELT Events English', kind='events', manifest='lastupdate.txt', suffix='.export.CSV.zip'),
    'events_translation': dict(label='GDELT Events Translingual', kind='events', manifest='lastupdate-translation.txt', suffix='.translation.export.CSV.zip'),
    'mentions_english': dict(label='GDELT Mentions English', kind='mentions', manifest='lastupdate.txt', suffix='.mentions.CSV.zip'),
    'mentions_translation': dict(label='GDELT Mentions Translingual', kind='mentions', manifest='lastupdate-translation.txt', suffix='.translation.mentions.CSV.zip'),
}


def kind_for(feed):
    if feed not in FEEDS: raise ValueError('unknown_intake_feed')
    return FEEDS[feed]['kind']


def permitted_members(feed, stamp):
    if feed not in FEEDS: raise ValueError('unknown_intake_feed')
    suffix = FEEDS[feed]['suffix'][:-4]  # The source ZIP has one data member.
    names = {stamp+suffix}
    if feed.endswith('_translation'):
        # GDELT has shipped translated archives with either member spelling.
        names.add(stamp+suffix.replace('.translation', '', 1))
    return names
