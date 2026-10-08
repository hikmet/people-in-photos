
import pytest

from app import storage


@pytest.fixture
def r2(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", "https://account.r2.cloudflarestorage.com")
    with moto.mock_aws():
        # R2 has no regions, so the bucket is created through a plain client; the backend under
        # test then talks to it through the R2-shaped endpoint that moto is told to intercept.
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="photos")
        yield storage.R2("account", "key", "secret", "photos")


def test_r2_round_trip(r2):
    assert not r2.exists("photos/x/original.jpg")
    with pytest.raises(KeyError):
        r2.get("photos/x/original.jpg")
    assert r2.response("photos/x/original.jpg", "image/jpeg", None).status_code == 404
    r2.put("photos/x/original.jpg", b"first")
    r2.put("photos/x/original.jpg", b"second")
    assert r2.exists("photos/x/original.jpg")
    assert r2.get("photos/x/original.jpg") == b"second"
    response = r2.response("photos/x/original.jpg", "image/jpeg", "photo.jpg")
    assert response.status_code == 200
    assert response.headers["content-length"] == "6"
    assert response.headers["content-disposition"] == 'attachment; filename="photo.jpg"'
    assert response.media_type == "image/jpeg"
    r2.delete("photos/x/original.jpg")
    r2.delete("photos/x/original.jpg")
    assert not r2.exists("photos/x/original.jpg")
    with pytest.raises(ValueError):
        r2.put("../escape", b"")


def test_local_round_trip(tmp_path):
    local = storage.Local(tmp_path)
    local.put("photos/x/original.jpg", b"first")
    local.put("photos/x/original.jpg", b"second")
    assert local.get("photos/x/original.jpg") == b"second"
    with pytest.raises(KeyError):
        local.get("photos/missing.jpg")
    assert local.response("photos/missing.jpg", "image/jpeg", None).status_code == 404
    local.delete("photos/x/original.jpg")
    local.delete("photos/x/original.jpg")
    assert not local.exists("photos/x/original.jpg")
