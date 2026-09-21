"""Milvus Lite 向量库:条款库/法条库的稠密+稀疏双路检索(RRF 融合)。

连接策略:模块级缓存单个 MilvusClient(threading.Lock 保护)。
pymilvus 的 gRPC 通道写死了 keepalive_time_ms=10000 +
keepalive_permit_without_calls=True,空闲时也会每 10s 发心跳;
Milvus Lite 服务端 max_pings_without_data 很小,会直接 GOAWAY
(too_many_pings) 把连接踹掉。因此 hybrid_search 捕获 MilvusException
后重建连接重试一次,避免把异常当成检索结果返回给 LLM。
"""
import threading
from typing import Any, Dict, List

from pymilvus import (
    AnnSearchRequest,
    DataType,
    MilvusClient,
    MilvusException,
    RRFRanker,
)

from .config import DIM, MILVUS_URI

_client: MilvusClient | None = None
_client_lock = threading.Lock()


def get_client(reset: bool = False) -> MilvusClient:
    """取缓存的 MilvusClient;reset=True 时先关掉旧连接再建新连接。"""
    global _client
    with _client_lock:
        if reset and _client is not None:
            try:
                _client.close()
            except Exception:
                pass
            _client = None
        if _client is None:
            _client = MilvusClient(uri=MILVUS_URI)
        return _client


def _schema_with_vectors(client: MilvusClient, name: str) -> None:
    schema = client.create_schema(auto_id=True)
    schema.add_field("pk", DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field("metadata", DataType.JSON)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=DIM)
    schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
    index_params = client.prepare_index_params()
    index_params.add_index("vector", index_type="AUTOINDEX", metric_type="L2")
    index_params.add_index("sparse_vector", index_type="SPARSE_INVERTED_INDEX", metric_type="IP")
    client.create_collection(name, schema=schema, index_params=index_params)


def ensure_collection(client: MilvusClient, name: str, reset: bool = False) -> None:
    if client.has_collection(name):
        if not reset:
            client.load_collection(name)
            return
        client.drop_collection(name)
    _schema_with_vectors(client, name)
    client.load_collection(name)


def insert_texts(
    client: MilvusClient,
    coll_name: str,
    texts: List[str],
    metas: List[Dict[str, Any]],
    model: Any,
) -> int:
    """用 bge-m3 编码 texts,稠密+稀疏向量一起入库。"""
    out = model.encode(texts, return_dense=True, return_sparse=True)
    rows = [
        {
            "vector": out["dense_vecs"][i].tolist(),
            "sparse_vector": out["lexical_weights"][i],
            "text": texts[i],
            "metadata": metas[i],
        }
        for i in range(len(texts))
    ]
    client.insert(coll_name, rows)
    return client.get_collection_stats(coll_name).get("row_count", 0)


def _ensure_loaded(client: MilvusClient, coll_name: str) -> None:
    """Milvus Lite 每次新连接后集合默认 released,检索前必须 load(幂等)。"""
    st = client.get_load_state(coll_name)
    state = st.get("state") if isinstance(st, dict) else getattr(st, "name", None)
    if state != "Loaded":
        client.load_collection(coll_name)


def _hybrid_search_once(
    client: MilvusClient,
    coll_name: str,
    query: str,
    model: Any,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """一次检索:稠密检索 + 稀疏检索,RRF 融合排名。"""
    _ensure_loaded(client, coll_name)
    out = model.encode([query], return_dense=True, return_sparse=True)
    dense_vec = out["dense_vecs"][0]
    sparse_vec = out["lexical_weights"][0]

    dense_req = AnnSearchRequest(
        data=[dense_vec], anns_field="vector",
        param={"metric_type": "L2"}, limit=limit * 2,
    )
    sparse_req = AnnSearchRequest(
        data=[sparse_vec], anns_field="sparse_vector",
        param={"metric_type": "IP"}, limit=limit * 2,
    )
    results = client.hybrid_search(
        coll_name,
        reqs=[dense_req, sparse_req],
        ranker=RRFRanker(k=60),
        limit=limit,
        output_fields=["text", "metadata"],
    )
    hits = []
    for hit in results[0]:
        entity = hit.get("entity", {})
        hits.append(
            {
                "distance": round(hit.get("distance", 0), 4),
                "text": entity.get("text", ""),
                "metadata": entity.get("metadata", {}),
            }
        )
    return hits


def hybrid_search(
    client: MilvusClient,
    coll_name: str,
    query: str,
    model: Any,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """稠密+稀疏 RRF 检索;连接被 gRPC keepalive 踢掉时自动重连重试一次。"""
    try:
        return _hybrid_search_once(client, coll_name, query, model, limit)
    except MilvusException as e:
        print(f"[milvus] 连接已失效({e});重建连接后重试一次", flush=True)
        fresh = get_client(reset=True)
        return _hybrid_search_once(fresh, coll_name, query, model, limit)
