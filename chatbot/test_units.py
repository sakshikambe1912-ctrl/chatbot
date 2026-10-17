from django.test import SimpleTestCase

from chatbot.services import clean_reply, fmt_price, prices_ok


class PricesOkTests(SimpleTestCase):
    def test_price_in_the_data_is_accepted(self):
        self.assertTrue(prices_ok("It costs ₹1,500.", [{"price": 1500}]))
        self.assertTrue(prices_ok("Rs. 999 or INR 1,500", [{"price": 999}, {"price": 1500}]))

    def test_invented_price_is_rejected(self):
        self.assertFalse(prices_ok("It costs ₹999.", [{"price": 1500}]))

    def test_reply_without_prices_is_fine(self):
        self.assertTrue(prices_ok("A lovely pair of shoes.", []))


class CleanReplyTests(SimpleTestCase):
    def test_markdown_is_removed(self):
        self.assertEqual(clean_reply("**Hi** `there`"), "Hi there")
        self.assertEqual(clean_reply("# Title\ntext"), "Title\ntext")
        self.assertEqual(clean_reply("- one\n- two"), "one\ntwo")

    def test_extra_blank_lines_are_collapsed(self):
        self.assertEqual(clean_reply("a\n\n\n\nb"), "a\n\nb")


class FmtPriceTests(SimpleTestCase):
    def test_formats(self):
        self.assertEqual(fmt_price(1500, "INR"), "₹1,500")
        self.assertEqual(fmt_price(1499.5, "USD"), "$1,499.50")
        self.assertEqual(fmt_price(10, "EUR"), "EUR 10")
        self.assertEqual(fmt_price("abc", "INR"), "₹abc")
