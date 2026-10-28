from omniarr import parser as P


def t(name, **exp):
    p = P.parse(name)
    for k, v in exp.items():
        got = getattr(p, k)
        if isinstance(got, set):
            got = sorted(got)
        assert got == v, f"{name}: {k} expected {v!r} got {got!r} ({p.to_dict()})"
    return p


def test_tv_basic():
    t("Glass.Harbor.S02E04.1080p.WEB-DL.DDP5.1.H.264-NTb", title="Glass Harbor", seasons=[2], episodes=[4],
      quality="WEBDL-1080p", group="NTb", languages=[])
    t("The.Office.US.S05E14.720p.HDTV.x264-CTU", title="The Office US", seasons=[5], episodes=[14],
      quality="HDTV-720p", group="CTU")
    t("Show Name - S01E01E02 - Pilot [1080p BluRay x265]", title="Show Name", episodes=[1, 2],
      quality="Bluray-1080p")
    t("Show.Name.S01E01-E03.720p.WEBRip.x264", episodes=[1, 2, 3], quality="WEBRip-720p")
    t("Show.Name.S01E05-07.1080p.AMZN.WEB-DL", episodes=[5, 6, 7], streaming="AMZN")
    t("show_name.1x05.hdtv", title="show name", seasons=[1], episodes=[5], quality="SDTV")
    t("Doctor.Who.2005.S13E01.1080p.WEB.h264-GOSSIP", title="Doctor Who", year=2005, seasons=[13])
    t("Show.S00E03.Special.1080p.WEB.H264-X", special=True, seasons=[0])
    t("Show.Name.S02E01.REPACK.1080p.WEB.H264-GRP", revision=2)


def test_packs():
    t("Glass.Harbor.S01.1080p.WEB-DL.DDP5.1-NTb", seasons=[1], full_season=True, episodes=[])
    t("Glass Harbor Season 2 Complete 720p BluRay", seasons=[2], full_season=True, quality="Bluray-720p")
    t("Show.S01-S03.1080p.BluRay.x264-GRP", seasons=[1, 2, 3], multi_season=True)
    t("The.Show.Complete.Series.1080p.BluRay", complete_series=True, title="The Show")


def test_daily_and_anime():
    t("The.Daily.Show.2024.01.15.Guest.720p.WEB.h264-EDITH", air_date="2024-01-15", title="The Daily Show")
    a = t("[SubsPlease] Frieren - 13 (1080p) [ABCD1234].mkv", absolute=[13], title="Frieren", group="SubsPlease",
          anime_style=True, quality="WEBDL-1080p")
    assert a.subbed is False
    t("[Erai-raws] Show Name - 01 ~ 12 [1080p][Multiple Subtitle]", title="Show Name")
    t("[Judas] Show Name - 1050 [1080p][HEVC x265 10bit][Dual-Audio]", absolute=[1050], dual_audio=True)


def test_movies():
    t("Northbound.2025.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX", title="Northbound", year=2025,
      quality="WEBDL-2160p", group="FLUX")
    t("Blade Runner 2049 (2017) [1080p] [BluRay]", title="Blade Runner 2049", year=2017, quality="Bluray-1080p")
    t("1917.2019.1080p.BluRay.x264-SPARKS", title="1917", year=2019)
    t("2012.2009.720p.BluRay", title="2012", year=2009)
    t("Some.Movie.2023.REMUX.2160p.UHD.BluRay", quality="Remux-2160p")
    t("Some.Movie.2023.HDCAM.x264", source="cam")
    t("Some.Movie.2024.HDTS.1080p", source="telesync")
    t("Some.Movie.2024.1080p.HC.WEBRip", hardcoded_subs=True)


def test_language_tags():
    t("Movie.2020.FRENCH.1080p.WEB.H264-GRP", languages=["fr"])
    t("Movie.2020.MULTi.1080p.BluRay.x264-GRP", dual_audio=True)
    t("Movie.2020.German.DL.1080p.BluRay.x264-GRP", languages=["de"], dual_audio=True)
    t("Movie.2020.ITA.ENG.1080p.BluRay", languages=["it"], has_english_tag=True)
    t("Show.S01E02.Dan.Goes.Home.1080p.WEB.H264-GRP", languages=[])       # episode title, not Danish
    t("The.French.Dispatch.2021.1080p.BluRay.x264", languages=[])          # title word, not a tag
    t("Movie.2020.1080p.WEB-DL.Hindi.DD5.1", languages=["hi"])
    t("Movie.2020.VOSTFR.1080p.WEB", languages=[], subbed=True)
    t("[Group] Anime - 05 [English Dub][1080p]", dubbed=True)


def lang(name, orig="en", mode="english", anime=False):
    return P.language_decision(P.parse(name), orig, mode, anime, True)


def test_language_decisions():
    assert lang("Glass.Harbor.S02E04.1080p.WEB-DL-NTb")[0]
    assert not lang("Glass.Harbor.S02E04.FRENCH.1080p.WEB")[0]
    assert lang("Glass.Harbor.S02E04.MULTi.1080p.WEB")[0]
    assert lang("Glass.Harbor.S02E04.FRENCH.1080p.WEB", mode="any")[0]
    # anime: original Japanese — subs-only rejected, dub/dual accepted
    assert not lang("[SubsPlease] Frieren - 13 (1080p)", "ja", anime=True)[0]
    ok, _, score_dub = lang("[Group] Frieren - 13 [English Dub][1080p]", "ja", anime=True)
    assert ok and score_dub >= 2
    assert lang("[Judas] Frieren - 13 [1080p][Dual-Audio]", "ja", anime=True)[0]
    # foreign film needs English dub in english mode
    assert not lang("Parasite.2019.1080p.BluRay.x264-GRP", "ko")[0]
    assert lang("Parasite.2019.1080p.BluRay.x264-GRP", "ko", mode="any")[0]
