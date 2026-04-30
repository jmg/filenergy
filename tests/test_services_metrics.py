from filenergy.services import metrics


def setup_function(_):
    metrics.reset()


def test_inc_counter_renders():
    metrics.inc("test_counter", {"foo": "bar"})
    metrics.inc("test_counter", {"foo": "bar"})
    out = metrics.render()
    assert "# TYPE test_counter counter" in out
    assert 'test_counter{foo="bar"} 2' in out


def test_inc_no_labels():
    metrics.inc("plain_counter")
    out = metrics.render()
    assert "plain_counter 1" in out


def test_observe_histogram_renders_buckets():
    metrics.observe("dur", 0.005)
    metrics.observe("dur", 0.5)
    metrics.observe("dur", 5.0)
    out = metrics.render()
    assert "# TYPE dur histogram" in out
    assert "dur_count 3" in out
    assert "dur_sum 5.505" in out
    assert 'dur_bucket{le="+Inf"} 3' in out


def test_render_is_empty_after_reset():
    metrics.inc("x")
    metrics.reset()
    assert metrics.render().strip() == ""


def test_observe_with_labels():
    metrics.observe("d", 0.1, {"endpoint": "x"})
    out = metrics.render()
    assert 'd_count{endpoint="x"} 1' in out
