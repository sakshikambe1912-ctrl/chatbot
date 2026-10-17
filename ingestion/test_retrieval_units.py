from unittest import mock

from django.contrib.postgres.search import CombinedSearchQuery, SearchQuery
from django.core.cache import cache
from django.test import SimpleTestCase

from ingestion import retrieval


class RrfTests(SimpleTestCase):
    def test_item_found_by_both_lists_ranks_first(self):
        merged = retrieval.rrf([[1, 2, 3], [3, 4]])
        self.assertEqual(merged[0], 3)
        self.assertEqual(set(merged), {1, 2, 3, 4})

    def test_empty_lists(self):
        self.assertEqual(retrieval.rrf([]), [])
        self.assertEqual(retrieval.rrf([[], []]), [])

    def test_single_list_keeps_its_order(self):
        self.assertEqual(retrieval.rrf([[7, 8, 9]]), [7, 8, 9])

    def test_one_empty_list_is_fine(self):
        self.assertEqual(retrieval.rrf([[], [5]]), [5])


class KeywordQueryTests(SimpleTestCase):
    def test_several_words_are_combined_with_or(self):
        self.assertIsInstance(retrieval._or_query("black running shoes"), CombinedSearchQuery)

    def test_one_word_is_a_plain_query(self):
        self.assertIsInstance(retrieval._or_query("shoes"), SearchQuery)

    def test_only_short_words_gives_none(self):
        self.assertIsNone(retrieval._or_query("a to be"))
        self.assertIsNone(retrieval._or_query(""))


class EmbeddingCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_repeated_query_calls_ollama_once(self):
        with mock.patch.object(retrieval, "embed_batch", return_value=[[0.1, 0.2]]) as embed:
            first = retrieval.embed_query("Black Running Shoes")
            second = retrieval.embed_query("  black   running shoes ")
        self.assertEqual(first, second)
        embed.assert_called_once()

    def test_different_queries_are_not_shared(self):
        with mock.patch.object(retrieval, "embed_batch", side_effect=[[[1.0]], [[2.0]]]) as embed:
            a = retrieval.embed_query("return policy")
            b = retrieval.embed_query("shipping policy")
        self.assertNotEqual(a, b)
        self.assertEqual(embed.call_count, 2)

    def test_a_failure_is_not_cached(self):
        with mock.patch.object(retrieval, "embed_batch", side_effect=[RuntimeError("ollama down"), [[0.5]]]):
            with self.assertRaises(RuntimeError):
                retrieval.embed_query("warranty")
            self.assertEqual(retrieval.embed_query("warranty"), [0.5])
