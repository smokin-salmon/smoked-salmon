from salmon import cfg
from salmon.uploader.upload import generate_source_links, generate_t_description


def test_generate_source_links_excludes_source_url() -> None:
    source_url = "https://gammenterprises.bandcamp.com/album/cry-fi-dem"
    metadata_urls = [
        source_url,
        "https://www.juno.co.uk/products/riddim-research-lab-vs-lay-cry-fi-dem-vinyl/1094887-01/",
        "https://wordandsound.net/release/160089-GAMM194-Riddim-Research-Lab-vs-Lay-Far--Ant-To-Be-Cry-Fi-Dem",
    ]

    links = generate_source_links(metadata_urls, source_url)

    assert "Bandcamp" not in links
    assert "juno.co.uk" in links
    assert "wordandsound.net" in links


def test_generate_t_description_omits_empty_more_info_after_source_filter() -> None:
    original_icons_in_descriptions = cfg.upload.description.icons_in_descriptions
    original_include_tracklist_in_t_desc = cfg.upload.description.include_tracklist_in_t_desc

    try:
        cfg.upload.description.icons_in_descriptions = False
        cfg.upload.description.include_tracklist_in_t_desc = True

        source_url = "https://gammenterprises.bandcamp.com/album/cry-fi-dem"
        description = generate_t_description(
            metadata={"date": "2025-07-25"},
            track_data={
                "01. Cry Fi Dem (vs Lay-Far).flac": {
                    "duration": 321,
                    "bit rate": 0,
                    "precision": 24,
                    "sample rate": 44100,
                }
            },
            hybrid=False,
            metadata_urls=[source_url],
            spectral_urls=None,
            spectral_ids=None,
            lossy_comment=None,
            source_url=source_url,
        )
    finally:
        cfg.upload.description.icons_in_descriptions = original_icons_in_descriptions
        cfg.upload.description.include_tracklist_in_t_desc = original_include_tracklist_in_t_desc

    assert "[b]Source:[/b] [url=https://gammenterprises.bandcamp.com/album/cry-fi-dem]Bandcamp[/url]" in description
    assert "[b]More info:[/b]" not in description
