"""The MintPy options a person may change: what is accepted and what is refused."""
import pytest

from insar_core.pipeline import mintpy_options as mo


def test_every_listed_option_has_what_the_form_needs():
    for option in mo.OPTIONS:
        assert option["key"].startswith(("mintpy.", mo.APP_PREFIX))
        assert option["kind"] in ("choice", "count", "length", "fraction")
        assert option["label"] and option["help"] and option["group"]
        if option["kind"] == "choice":
            assert "auto" in option["choices"]
    assert len({o["key"] for o in mo.OPTIONS}) == len(mo.OPTIONS)


def test_the_steps_this_app_makes_itself_are_checked_like_the_rest_and_kept_out_of_mintpys_config():
    checked = mo.validate_options({mo.MASK_CLOSURES_KEY: "no", mo.MASK_COHERENCE_KEY: "0.50", "mintpy.deramp": "linear"})
    mintpy, app = mo.split_app_options(checked)
    assert mintpy == {"mintpy.deramp": "linear"}
    assert app == {mo.MASK_CLOSURES_KEY: "no", mo.MASK_COHERENCE_KEY: "0.5"}
    with pytest.raises(ValueError, match="between 0 and 1"):
        mo.validate_options({mo.MASK_COHERENCE_KEY: "1.5"})


def test_good_values_are_normalised_and_auto_is_dropped():
    checked = mo.validate_options({
        "mintpy.troposphericDelay.method": "height_correlation",
        "mintpy.network.tempBaseMax": " 48 ",
        "mintpy.network.perpBaseMax": "150.0",
        "mintpy.networkInversion.minTempCoh": "0.80",
        "mintpy.deramp": "auto",
        "mintpy.network.connNumMax": "no",
    })
    assert checked == {
        "mintpy.troposphericDelay.method": "height_correlation",
        "mintpy.network.tempBaseMax": "48",
        "mintpy.network.perpBaseMax": "150",
        "mintpy.networkInversion.minTempCoh": "0.8",
        "mintpy.network.connNumMax": "no",
    }


def test_nothing_requested_means_nothing_changed():
    assert mo.validate_options(None) == {} and mo.validate_options({}) == {}


@pytest.mark.parametrize("options, message", [
    ({"mintpy.load.unwFile": "x"}, "not an option"),
    ({"mintpy.deramp": "cubic"}, "Remove a ramp"),
    ({"mintpy.network.tempBaseMax": "0"}, "at least 1"),
    ({"mintpy.network.tempBaseMax": "ten"}, "whole number"),
    ({"mintpy.network.tempBaseMax": "2.5"}, "whole number"),
    ({"mintpy.network.perpBaseMax": "-3"}, "above 0"),
    ({"mintpy.network.perpBaseMax": "nan"}, "above 0"),
    ({"mintpy.networkInversion.minTempCoh": "1.5"}, "between 0 and 1"),
    ({"mintpy.networkInversion.minTempCoh": "high"}, "number from 0 to 1"),
])
def test_bad_values_are_refused_with_a_message_that_names_the_option(options, message):
    with pytest.raises(ValueError, match=message):
        mo.validate_options(options)


def test_the_reference_point_becomes_the_option_mintpy_reads():
    assert mo.reference_option({"lat": 37.65, "lon": -1.67}) == {"mintpy.reference.lalo": "37.650000,-1.670000"}
    assert mo.reference_option(None) == {}


@pytest.mark.parametrize("reference", [{"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": "a", "lon": 1}, {"lat": 1}])
def test_a_reference_point_off_the_globe_or_incomplete_is_refused(reference):
    with pytest.raises(ValueError):
        mo.reference_option(reference)


def test_the_ionosphere_option_is_only_for_engines_that_have_the_layer():
    nisar = {o["key"] for o in mo.options_for("nisar_gunw")}
    hyp3 = {o["key"] for o in mo.options_for("hyp3_gamma")}
    assert "mintpy.ionosphericDelay.method" in nisar and "mintpy.ionosphericDelay.method" not in hyp3
    assert hyp3 < nisar and {o["key"] for o in mo.options_for(None)} == hyp3
    assert mo.validate_options({"mintpy.ionosphericDelay.method": "no"}, "nisar_gunw") == {"mintpy.ionosphericDelay.method": "no"}
    with pytest.raises(ValueError, match="not an option"):
        mo.validate_options({"mintpy.ionosphericDelay.method": "no"}, "hyp3_gamma")
    with pytest.raises(ValueError, match="not an option"):
        mo.validate_options({"mintpy.ionosphericDelay.method": "no"})
