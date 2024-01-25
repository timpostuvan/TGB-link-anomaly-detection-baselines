from typing import Dict, Optional, List
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from tqdm import tqdm
import numpy as np
import logging
import time
import argparse
import os
import json

from models.EdgeBank import edge_bank_link_prediction
from utils.metrics import get_link_prediction_metrics, get_node_classification_metrics
from utils.utils import set_random_seed
from utils.utils import NeighborSampler
from utils.DataLoader import Data

# Additional required imports
from tgb.linkproppred.evaluate import Evaluator


def query_pred_edge_batch(
    model_name: str,
    model: nn.Module,
    src_node_ids: int,
    dst_node_ids: int,
    node_interact_times: float,
    edge_ids: int,
    edges_are_positive: bool,
    num_neighbors: int,
    time_gap: int,
):
    """
    Query the prediction probabilities for a batch of edges.
    """
    if model_name in ["TGAT", "CAWN", "TCL"]:
        # Get temporal embedding of source and destination nodes.
        # Two Tensors, with shape (batch_size, node_feat_dim)
        batch_src_node_embeddings, batch_dst_node_embeddings = model[
            0
        ].compute_src_dst_node_temporal_embeddings(
            src_node_ids=src_node_ids,
            dst_node_ids=dst_node_ids,
            node_interact_times=node_interact_times,
            num_neighbors=num_neighbors,
        )

    elif model_name in ["JODIE", "DyRep", "TGN"]:
        # Get temporal embedding of source and destination nodes.
        # Two Tensors, with shape (batch_size, node_feat_dim).
        batch_src_node_embeddings, batch_dst_node_embeddings = model[
            0
        ].compute_src_dst_node_temporal_embeddings(
            src_node_ids=src_node_ids,
            dst_node_ids=dst_node_ids,
            node_interact_times=node_interact_times,
            edge_ids=edge_ids,
            edges_are_positive=edges_are_positive,
            num_neighbors=num_neighbors,
        )

    elif model_name in ["GraphMixer"]:
        # Get temporal embedding of source and destination nodes.
        # Two Tensors, with shape (batch_size, node_feat_dim)
        batch_src_node_embeddings, batch_dst_node_embeddings = model[
            0
        ].compute_src_dst_node_temporal_embeddings(
            src_node_ids=src_node_ids,
            dst_node_ids=dst_node_ids,
            node_interact_times=node_interact_times,
            num_neighbors=num_neighbors,
            time_gap=time_gap,
        )

    elif model_name in ["DyGFormer"]:
        # Get temporal embedding of source and destination nodes.
        # Two Tensors, with shape (batch_size, node_feat_dim)
        batch_src_node_embeddings, batch_dst_node_embeddings = model[
            0
        ].compute_src_dst_node_temporal_embeddings(
            src_node_ids=src_node_ids,
            dst_node_ids=dst_node_ids,
            node_interact_times=node_interact_times,
        )

    else:
        raise ValueError(f"Wrong value for model_name {model_name}!")
        batch_src_node_embeddings, batch_dst_node_embeddings = None, None

    return batch_src_node_embeddings, batch_dst_node_embeddings


def eval_linkpred_TGB(
    model_name: str,
    model: nn.Module,
    neighbor_sampler: NeighborSampler,
    evaluate_idx_data_loader: DataLoader,
    evaluate_data: Data,
    min_dst_idx: int,
    max_dst_idx: int,
    evaluator: Evaluator,
    metrics: Optional[List[str]] = ["auc"],
    num_neighbors: Optional[int] = 20,
    time_gap: Optional[int] = 2000,
    show_progress_bar: Optional[bool] = False,
) -> Dict[str, float]:
    """
    Evaluate models on the link anomaly detection task based on TGB Evaluator.
    Args:
        model_name (str): Name of the model.
        model (nn.Module): The model to be evaluated.
        neighbor_sampler (NeighborSampler): Neighbor sampler.
        evaluate_idx_data_loader (DataLoader): Evaluate index data loader.
        evaluate_data (Data): Data on which to evaluate the model.
        min_dst_idx (int): Minimal destination node id.
        max_dst_idx (int): Maximal destination node id.
        evaluator (Evaluator): Dynamic link anomaly detection evaluator.
        metrics (Optional[List[str]], optional): Evaluation metrics. Defaults to ["auc"].
        num_neighbors (Optional[int], optional): Number of neighbors to sample for each node. Defaults to 20.
        time_gap (Optional[int], optional): Time gap for neighbors to compute node features. Defaults to 2000.
        show_progress_bar (Optional[bool], optional): Whether to show progress bar during evaluation. Defaults to False.

    Returns:
        Dict[str, float]: Dictonary with results.
    """
    perf_list = []

    if model_name in ["DyRep", "TGAT", "TGN", "CAWN", "TCL", "GraphMixer", "DyGFormer"]:
        # Evaluation phase uses all the graph information.
        model[0].set_neighbor_sampler(neighbor_sampler)

    model.eval()

    y_preds, y_labels = [], []
    with torch.no_grad():
        # Store evaluate losses and metrics.
        evaluate_losses, evaluate_metrics = [], []
        evaluate_idx_data_loader_tqdm = tqdm(
            evaluate_idx_data_loader,
            ncols=120,
            disable=(not show_progress_bar),
        )
        for batch_idx, evaluate_data_indices in enumerate(
            evaluate_idx_data_loader_tqdm
        ):
            (
                batch_src_node_ids,
                batch_dst_node_ids,
                batch_node_interact_times,
                batch_edge_ids,
                batch_labels,
            ) = (
                evaluate_data.src_node_ids[evaluate_data_indices],
                evaluate_data.dst_node_ids[evaluate_data_indices],
                evaluate_data.node_interact_times[evaluate_data_indices],
                evaluate_data.edge_ids[evaluate_data_indices],
                evaluate_data.labels[evaluate_data_indices],
            )
            batch_msg_feat = model[0].edge_raw_features[batch_edge_ids]

            assert np.all(
                batch_labels == 1
            ), "Validation set should not contain anomalous edges!"

            # Create negative edges by randomly sampling destinations.
            batch_neg_src_node_ids = np.copy(batch_src_node_ids)
            batch_neg_dst_node_ids = np.random.randint(
                min_dst_idx,
                max_dst_idx + 1,
                size=batch_neg_src_node_ids.shape[0],
            )
            batch_neg_node_interact_times = np.copy(batch_node_interact_times)
            batch_neg_msg_feat = batch_msg_feat.clone()

            # In link prediction task, the model knows which links are benign and which are anomalous,
            # therefore, node memories should be updated according to only benign edges.
            # This is achieved by setting edges_are_positive to True.
            (
                batch_src_node_embeddings,
                batch_dst_node_embeddings,
            ) = query_pred_edge_batch(
                model_name=model_name,
                model=model,
                src_node_ids=batch_src_node_ids,
                dst_node_ids=batch_dst_node_ids,
                node_interact_times=batch_node_interact_times,
                edge_ids=batch_edge_ids,
                edges_are_positive=True,
                num_neighbors=num_neighbors,
                time_gap=time_gap,
            )

            (
                batch_neg_src_node_embeddings,
                batch_neg_dst_node_embeddings,
            ) = query_pred_edge_batch(
                model_name=model_name,
                model=model,
                src_node_ids=batch_neg_src_node_ids,
                dst_node_ids=batch_neg_dst_node_ids,
                node_interact_times=batch_neg_node_interact_times,
                edge_ids=None,
                edges_are_positive=False,
                num_neighbors=num_neighbors,
                time_gap=time_gap,
            )

            # Get prediction probabilities.
            batch_preds = (
                model[1](
                    node_embedding1=batch_src_node_embeddings,
                    node_embedding2=batch_dst_node_embeddings,
                    context=batch_msg_feat,
                )
                .squeeze(dim=-1)
                .sigmoid()
            )

            batch_neg_preds = (
                model[1](
                    node_embedding1=batch_neg_src_node_embeddings,
                    node_embedding2=batch_neg_dst_node_embeddings,
                    context=batch_neg_msg_feat,
                )
                .squeeze(dim=-1)
                .sigmoid()
            )

            y_preds.append(torch.cat([batch_preds, batch_neg_preds], dim=0).cpu())
            y_labels.append(
                torch.cat(
                    [torch.ones_like(batch_preds), torch.zeros_like(batch_neg_preds)],
                    dim=0,
                ).cpu()
            )

    # Compute evaluation metrics.
    input_dict = {
        "y_pred": np.concatenate(y_preds),
        "y_label": np.concatenate(y_labels),
        "eval_metric": metrics,
    }
    perf_metrics = evaluator.eval(input_dict)
    return perf_metrics
