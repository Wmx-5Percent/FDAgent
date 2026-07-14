"""Generate README diagrams that explain the FDAgent /ask workflow."""
from __future__ import annotations

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.generic.blank import Blank
from diagrams.onprem.analytics import Spark
from diagrams.onprem.client import User
from diagrams.onprem.compute import Server
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.inmemory import Redis
from diagrams.onprem.monitoring import Grafana


HERE = Path(__file__).resolve().parent
GRAPH_ATTR = {
    "bgcolor": "transparent",
    "pad": "0.25",
    "ranksep": "0.75",
    "nodesep": "0.45",
    "splines": "ortho",
}
NODE_ATTR = {
    "fontname": "Helvetica",
    "fontsize": "11",
}
EDGE_ATTR = {
    "fontname": "Helvetica",
    "fontsize": "10",
}


def project_node(label: str) -> Blank:
    """Use neutral nodes so labels can name project files precisely."""
    return Blank(label)


def render_request_routing() -> None:
    with Diagram(
        "FDAgent /ask request routing",
        filename=str(HERE / "agent_request_routing"),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=GRAPH_ATTR,
        node_attr=NODE_ATTR,
        edge_attr=EDGE_ATTR,
    ):
        user = User("User question")
        ui = Server("web/*\nchat UI")
        api = Server("src/api.py\nPOST /ask")

        with Cluster("NLEngine.ask()"):
            guard = project_node("agent_control.py\nLLM intent gate")
            terminal = project_node("terminal routes\nmeta / out-of-domain /\nclarification")
            spec = project_node("nl_query.py\nvalidated QuerySpec")

        with Cluster("Current answer paths"):
            sql = PostgreSQL("analytics.py\nread-only SQL\ncounts/trends/tables")
            taxonomy = project_node("taxonomy + recall_label\nexplain / exact counts")
            retrieval = project_node("retrieval.py\npgvector + FTS + RRF")
            validation = project_node("validation.py\nsemantic count\nLLM yes/no")
            multi = project_node("multi-section planner\nfirm-scoped reason + product Top-N")

        response = project_node("answer payload\nsummary + chart/table data\n+ evidence links")
        query_log = Grafana("observability.py\nquery_log")

        user >> ui >> api >> guard
        guard >> Edge(label="chitchat/meta\nout-of-domain\nambiguous") >> terminal >> response
        guard >> Edge(label="in_domain") >> spec
        spec >> Edge(label="deterministic\nnumbers") >> sql >> response
        spec >> Edge(label="taxonomy route") >> taxonomy >> response
        spec >> Edge(label="fuzzy concept\nexamples") >> retrieval >> response
        retrieval >> Edge(label="count-style\nconcept query") >> validation >> response
        spec >> Edge(label="reason + product\nTop-N") >> multi >> response
        response >> ui
        response >> Edge(style="dashed", label="trace") >> query_log


def render_component_map() -> None:
    with Diagram(
        "FDAgent serving-path component map",
        filename=str(HERE / "agent_component_map"),
        outformat="png",
        show=False,
        direction="TB",
        graph_attr=GRAPH_ATTR,
        node_attr=NODE_ATTR,
        edge_attr=EDGE_ATTR,
    ):
        with Cluster("Serving surface"):
            ui = Server("web/index.html\nweb/app.js\nweb/styles.css")
            api = Server("src/api.py\nFastAPI /ask\nserialization + evidence URLs")

        with Cluster("Agent control and planning"):
            guard = project_node("agent_control.py\nroute guard")
            planner = project_node("nl_query.py\nQuerySpec generation,\nrefinement, routing")

        with Cluster("Tools the planner may call"):
            analytics = PostgreSQL("analytics.py\nSQL analytics")
            taxonomy = project_node("taxonomy sidecar\nrecall_label exact categories")
            retrieval = Spark("retrieval.py\nhybrid retrieval")
            validator = project_node("validation.py\nsemantic validation")
            logger = Grafana("observability.py\nquery_log audit trail")

        with Cluster("Data stores"):
            drug = PostgreSQL("drug_enforcement\nopenFDA facts")
            labels = PostgreSQL("taxonomy / recall_label")
            embeddings = PostgreSQL("embeddings\npgvector + FTS")

        ui >> api >> guard >> planner
        planner >> analytics >> drug
        planner >> taxonomy >> labels
        planner >> retrieval >> embeddings
        retrieval >> validator
        api >> Edge(style="dashed", label="request/response metadata") >> logger


def render_trust_boundary() -> None:
    with Diagram(
        "FDAgent evidence and trust boundary",
        filename=str(HERE / "agent_trust_boundary"),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=GRAPH_ATTR,
        node_attr=NODE_ATTR,
        edge_attr=EDGE_ATTR,
    ):
        openfda = Redis("openFDA drug/enforcement\npublic-domain source")
        facts = PostgreSQL("drug_enforcement\nraw JSONB + parsed columns")

        with Cluster("Deterministic fact boundary"):
            sql = PostgreSQL("SQL owns numeric facts\ncounts, groups, trends")
            evidence = project_node("recall_number evidence\nopenFDA verification links")

        with Cluster("LLM-owned tasks"):
            route = project_node("routing + QuerySpec\nno raw counts")
            summary = project_node("summaries / semantic\nsnippet validation")

        with Cluster("Reliability metadata"):
            fallback = project_node("retrieval_mode\nhybrid vs fts_only")
            log = Grafana("query_log\nspec + decision + errors")

        future = project_node("Future recall profile\nparent/brand sidecars\nnot wired into /ask yet")
        answer = project_node("User-visible answer\nfacts + evidence,\nno safe/unsafe verdict")

        openfda >> facts >> sql >> answer
        route >> Edge(label="validated spec") >> sql
        route >> summary >> answer
        sql >> evidence >> answer
        summary >> fallback >> log
        sql >> log
        future >> Edge(style="dashed", label="planned, see PROGRESS.md") >> answer


def main() -> None:
    render_request_routing()
    render_component_map()
    render_trust_boundary()


if __name__ == "__main__":
    main()
