from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """
    Paginação padrão da API: 10 por página, ajustável com ?page_size= (o seletor "itens por
    página" do painel e as listas de opções dos formulários). O PageNumberPagination do DRF
    ignora page_size sem `page_size_query_param`, por isso as listas ficavam presas em 10.
    """

    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 500
