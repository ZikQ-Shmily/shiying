import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import share_scanner as scanner
from integrations import IntegrationError


class MovieShareScannerTests(unittest.TestCase):
    def setUp(self):
        scanner._CACHE.clear()

    @staticmethod
    def video(name, size=2 * 1024 ** 3):
        return dict(fc=1, fid='123', n=name, s=str(size))

    @staticmethod
    def directory(cid, name):
        return dict(fc=0, cid=str(cid), n=name)

    def read_pages(self, pages, **kwargs):
        def fetch(url, **unused):
            values = parse_qs(urlsplit(url).query)
            count, rows = pages[(values['cid'][0], values['offset'][0])]
            return dict(state=True, data=dict(count=count, list=rows))
        with patch.object(scanner, 'fetch', side_effect=fetch):
            return scanner.read_share_episodes('https://115.com/s/movie123', media_type='movie',
                                               tmdb_id=321, minimum_interval=0, **kwargs)

    def test_movie_video_and_duplicate_encodes_are_one_movie(self):
        rows = [self.video('空枪.2026.2160p.WEB-DL.mkv'), self.video('空枪.2026.1080p.mp4'),
                self.video('空枪.2026.CD1.avi'), self.video('空枪.2026.CD2.avi')]
        result = self.read_pages({('0', '0'): (4, rows)})
        self.assertEqual((result['state'], result['episodes'], result['episode_count']), ('complete', '1', 1))
        self.assertEqual(result['video_count'], 4)

    def test_subtitles_trailers_samples_and_episodic_content_do_not_count(self):
        rows = [self.video('空枪.2026.srt'), self.video('空枪.2026.trailer.mp4'),
                self.video('空枪_2026_sample_video.mkv'), self.video('空枪.花絮.mkv'),
                self.video('Series.S01E01.mkv'), self.video('Series.EP03.mkv'),
                self.video('综艺.第十二期.mp4'), self.video('正片.mp4', 1024)]
        result = self.read_pages({('0', '0'): (len(rows), rows)})
        self.assertEqual(result['state'], 'empty')
        self.assertEqual(result['episode_count'], 0)
        self.assertEqual(result['video_count'], 0)

    def test_nested_bluray_streams_count_and_other_tmdb_content_is_skipped(self):
        pages = {
            ('0', '0'): (1, [self.directory(1, '空枪 (2026) {tmdb-321}')]),
            ('1', '0'): (3, [self.directory(2, 'BDMV'), self.directory(5, 'Other {tmdb-999}'), self.directory(6, 'Extras')]),
            ('2', '0'): (1, [self.directory(3, 'STREAM')]),
            ('3', '0'): (3, [self.video('00001.m2ts'), self.video('00002.m2ts'), self.video('00003.m2ts', 1024)]),
        }
        result = self.read_pages(pages)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['episodes'], '1')
        self.assertEqual(result['video_count'], 2)
        self.assertTrue(scanner.skip_movie_path('/This {tmdb-321}/Other {tmdb-999}/Main.mkv', 321))

    def test_positive_file_does_not_hide_incomplete_directory_read(self):
        responses = [dict(state=True, data=dict(count=2, list=[self.video('空枪.mkv')])),
                     dict(state=True, data=dict(count=2, list=[]))]
        with patch.object(scanner, 'fetch', side_effect=responses), self.assertRaises(IntegrationError):
            scanner.read_share_episodes('https://115.com/s/movie123', media_type='movie', minimum_interval=0)

    def test_cache_does_not_mix_tv_and_movie_results(self):
        response = dict(state=True, data=dict(count=1, list=[self.video('Movie.2026.mkv')]))
        with patch.object(scanner, 'fetch', return_value=response) as fetch:
            tv = scanner.read_share_episodes('https://115.com/s/movie123', minimum_interval=0)
            movie = scanner.read_share_episodes('https://115.com/s/movie123', media_type='movie', minimum_interval=0)
            scanner.read_share_episodes('https://115.com/s/movie123', media_type='movie', minimum_interval=0)
        self.assertEqual(tv['state'], 'partial')
        self.assertEqual(movie['state'], 'complete')
        self.assertEqual(fetch.call_count, 2)

    def test_missing_size_can_read_but_invalid_size_and_small_clips_are_not_evidence(self):
        self.assertTrue(scanner.movie_video_candidate(dict(n='Main.mkv')))
        self.assertFalse(scanner.movie_video_candidate(dict(s=True)))
        self.assertFalse(scanner.movie_video_candidate(dict(s='1024MB')))
        self.assertFalse(scanner.movie_video_candidate(dict(s='100')))

    def test_invalid_media_type_is_rejected_without_network(self):
        with patch.object(scanner, 'fetch') as fetch, self.assertRaises(IntegrationError):
            scanner.read_share_episodes('https://115.com/s/movie123', media_type='bad')
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
