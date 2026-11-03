import unittest

from backend.app.services.jira import NO_CUSTOMER, JiraEscalationPublisher

KNOWN = {"auth-service", "notifications", "payments-api", "web-dashboard", "file-upload"}


class JiraImportFieldTests(unittest.TestCase):
    def fields(self, **ticket):
        return JiraEscalationPublisher.case_fields({"summary": "", "description": "", "reporter": "", **ticket}, KNOWN)

    def test_service_and_customer_lines_win(self):
        self.assertEqual(self.fields(description="Customer: Contoso Retail\nService: notifications", reporter="Ann"),
                         ("notifications", "Contoso Retail"))

    def test_component_or_label_gives_the_service(self):
        # the reporter wrote the ticket; that person is not the customer
        self.assertEqual(self.fields(components="payments-api", reporter="Ann"), ("payments-api", NO_CUSTOMER))
        self.assertEqual(self.fields(labels="urgent,web-dashboard"), ("web-dashboard", NO_CUSTOMER))

    def test_a_ticket_that_rootiq_made_names_its_customer(self):
        text = "RootIQ case for: Umbrella Fitness\nService: payments-api  |  Environment: production\nFive members..."
        self.assertEqual(self.fields(description=text, reporter="Ann"), ("payments-api", "Umbrella Fitness"))

    def test_service_name_in_the_text(self):
        self.assertEqual(self.fields(summary="auth-service rejects logins")[0], "auth-service")

    def test_no_hint_gives_unknown_not_the_project_key(self):
        self.assertEqual(self.fields(summary="Something is odd")[0], "unknown")


if __name__ == "__main__":
    unittest.main()
