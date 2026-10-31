from customer_mcp_server.data import CUSTOMERS, customer_exists, get_customer


def test_get_existing_customer():
    record = get_customer("CUST-00001")
    assert record is not None
    assert record["customer_id"] == "CUST-00001"
    assert record["name"] == "Ada Okafor"


def test_get_missing_customer_returns_none():
    assert get_customer("CUST-99999") is None


def test_returned_record_is_a_copy():
    record = get_customer("CUST-00001")
    record["name"] = "Changed"
    assert CUSTOMERS["CUST-00001"]["name"] == "Ada Okafor"


def test_customer_exists():
    assert customer_exists("CUST-00002")
    assert not customer_exists("CUST-99999")


def test_all_mock_ids_match_their_keys():
    for key, record in CUSTOMERS.items():
        assert record["customer_id"] == key
