import unittest
import numpy as np
import engine


class ShortSpeakerEvidenceTests(unittest.TestCase):
    def test_isolated_short_outlier_is_not_a_second_speaker(self):
        features = np.array([[0., 1.], [1., .02], [1., .03], [1., .01]])
        labels, report = engine.cluster_speakers(features, [.64, 12.7, 4.7, 1.75], None)
        self.assertEqual(report['count'], 1)
        self.assertEqual(len(set(labels)), 1)
        self.assertTrue(report['limited_evidence'])

    def test_known_count_is_not_suppressed_by_short_evidence(self):
        features = np.array([[0., 1.], [1., .02], [1., .03], [1., .01]])
        labels, report = engine.cluster_speakers(features, [.64, 12.7, 4.7, 1.75], 2)
        self.assertEqual(report['count'], 2)
        self.assertEqual(report['count_mode'], 'specified')
        self.assertEqual(len(set(labels)), 2)

    def test_repeated_short_answers_accumulate_sufficient_evidence(self):
        features = np.vstack([np.tile([1., 0.], (4, 1)), np.tile([0., 1.], (4, 1))])
        labels, report = engine.cluster_speakers(features, [4., 4., 4., 4., .6, .6, .6, .6], None)
        self.assertEqual(report['count'], 2)
        self.assertEqual(len(set(labels)), 2)

    def test_short_video_can_still_contain_two_supported_speakers(self):
        features = np.vstack([np.tile([1., 0.], (4, 1)), np.tile([0., 1.], (4, 1))])
        labels, report = engine.cluster_speakers(features, np.ones(8)*.6, None)
        self.assertEqual(report['count'], 2)
        self.assertEqual(len(set(labels)), 2)

    def test_short_clusters_below_threshold_do_not_add_speakers(self):
        features = np.vstack([np.tile([1., 0.], (4, 1)), np.tile([0., 1.], (3, 1))])
        labels, report = engine.cluster_speakers(features, [3., 3., 3., 3., .4, .4, .4], None)
        self.assertEqual(report['count'], 1)
        self.assertTrue(report['limited_evidence'])


if __name__ == '__main__':unittest.main()
