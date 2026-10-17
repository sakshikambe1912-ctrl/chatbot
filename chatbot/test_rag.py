from types import SimpleNamespace
from unittest import TestCase, mock

from chatbot import rag


def chunk(text, title="Return and Refund Policy"):
    return SimpleNamespace(text=text, meta={"title": title}, document_id=1)


RETURNS = chunk("# Return Policy\nItems can be returned within 7 days of delivery in original condition. "
                "Refunds are issued to the original payment method in 5 to 7 business days.")
STORE = SimpleNamespace(name="Shop", currency="INR")


class ChecksTests(TestCase):
    def test_citations(self):
        self.assertTrue(rag.citations_ok("Returns take 7 days [1].", 2))
        self.assertFalse(rag.citations_ok("Returns take 7 days [3].", 2))
        self.assertFalse(rag.citations_ok("Returns take 7 days [0].", 2))

    def test_numbers(self):
        ctx = "[1] Policy: returns within 7 days; refund in 5 to 7 days. Orders above 1,500 ship free."
        self.assertTrue(rag.numbers_ok("You have 7 days [1].", ctx))
        self.assertTrue(rag.numbers_ok("Free shipping above 1500 [1].", ctx))
        self.assertFalse(rag.numbers_ok("You have 30 days [1].", ctx))

    def test_excerpt_strips_heading_and_names_source(self):
        out = rag.excerpt(RETURNS)
        self.assertTrue(out.startswith("From our Return and Refund Policy: Return Policy"))
        self.assertNotIn("#", out)


class AnswerTests(TestCase):
    def run_answer(self, model_reply, chunks=None, **kw):
        chunks = [RETURNS] if chunks is None else chunks
        with mock.patch.object(rag, "retrieve", return_value=chunks), \
             mock.patch.object(rag, "_generate", return_value=model_reply):
            return rag.answer_with_sources(STORE, "what is the return policy?", **kw)

    def test_good_answer_keeps_reply_and_sources(self):
        reply, sources = self.run_answer("You can return items within 7 days [1].")
        self.assertEqual(reply, "You can return items within 7 days [1].")
        self.assertEqual(sources, [{"title": "Return and Refund Policy"}])

    def test_invented_number_falls_back_to_excerpt(self):
        reply, _ = self.run_answer("You can return items within 30 days [1].")
        self.assertTrue(reply.startswith("From our Return and Refund Policy"))

    def test_bad_citation_falls_back_to_excerpt(self):
        reply, _ = self.run_answer("You can return items within 7 days [2].")
        self.assertTrue(reply.startswith("From our Return and Refund Policy"))

    def test_nothing_found_uses_fallback_or_no_info(self):
        self.assertEqual(self.run_answer("x", chunks=[])[0], rag.NO_INFO)
        self.assertEqual(self.run_answer("x", chunks=[], fallback="custom")[0], "custom")

    def test_retrieval_error_uses_fallback(self):
        with mock.patch.object(rag, "retrieve", side_effect=RuntimeError("ollama down")):
            self.assertEqual(rag.answer_with_sources(STORE, "q")[0], rag.NO_INFO)

    def test_model_down_returns_excerpt(self):
        class Down(Exception):
            pass
        with mock.patch.object(rag, "retrieve", return_value=[RETURNS]), \
             mock.patch.object(rag, "_generate", side_effect=Down()), \
             mock.patch.object(rag, "_model_down_errors", return_value=(Down,)):
            reply, _ = rag.answer_with_sources(STORE, "q")
        self.assertTrue(reply.startswith("From our Return and Refund Policy"))

    def test_string_wrapper_appends_sources_line(self):
        with mock.patch.object(rag, "retrieve", return_value=[RETURNS]), \
             mock.patch.object(rag, "_generate", return_value="Within 7 days [1]."):
            out = rag.answer_with_rag(STORE, "returns?")
        self.assertTrue(out.endswith("Sources: Return and Refund Policy"))