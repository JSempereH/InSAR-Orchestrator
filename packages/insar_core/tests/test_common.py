

def test_a_pixel_cube_reads_only_the_pixels_asked_for_and_gives_what_the_whole_cube_would(tmp_path):
    import h5py
    import numpy as np

    from insar_core.common import PixelCube

    cube = np.random.default_rng(0).normal(size=(6, 9, 11)).astype("float32")
    with h5py.File(tmp_path / "timeseries.h5", "w") as handle:
        handle.create_dataset("timeseries", data=cube, chunks=(6, 1, 11))
    reader = PixelCube(tmp_path / "timeseries.h5", scale=1000.0)
    assert reader.shape == (6, 9, 11)
    rows, columns = np.array([4, 0, 4, 8]), np.array([3, 10, 7, 0])      # unsorted, one row twice
    np.testing.assert_allclose(reader[:, rows, columns], cube[:, rows, columns] * 1000.0, rtol=1e-6)
    np.testing.assert_allclose(reader[:, 2, 5], cube[:, 2, 5] * 1000.0, rtol=1e-6)
    assert reader[:, np.array([], int), np.array([], int)].shape == (6, 0)
