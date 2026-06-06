from src.api.routes import funds


def test_fund_list_routes_default_to_smaller_page_size():
    """基金列表接口默认分页大小应避免一次返回过多数据"""
    assert funds.get_funds.__defaults__[1] == 100
    assert funds.get_funds_by_sector.__defaults__[1] == 100
    assert funds.get_active_funds.__defaults__[1] == 100
