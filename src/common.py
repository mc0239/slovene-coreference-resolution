import logging
import os
import time

import metrics
import torch
import torch.nn as nn
from utils import get_clusters
from visualization import build_and_display

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ControllerBase:
    def __init__(self, learning_rate, dataset_name, model_name=None):
        self.model_name = time.strftime("%Y%m%d_%H%M%S") if model_name is None else model_name
        self.dataset_name = dataset_name
        self.learning_rate = learning_rate

        # Mention ranking model = always using cross-entropy
        self.loss = nn.CrossEntropyLoss()

        # Put various debugging/visualization related things in model dir
        self.path_model_dir = os.path.join(self.model_base_dir, self.model_name)
        self.path_metadata = os.path.join(self.path_model_dir, "model_metadata.txt")
        self.path_pred_clusters = os.path.join(self.path_model_dir, "pred_clusters.txt")
        self.path_pred_scores = os.path.join(self.path_model_dir, "pred_scores.txt")
        self.path_log = os.path.join(self.path_model_dir, "log.txt")

        self.loaded_from_file = False
        self._prepare()

    @property
    def model_base_dir(self):
        """ Should return the directory where models of this type should be saved. """
        raise NotImplementedError

    def _prepare(self):
        if os.path.exists(self.path_model_dir):
            self.load_checkpoint()
        else:
            os.makedirs(self.path_model_dir)
            logger.addHandler(logging.FileHandler(self.path_log, mode="w", encoding="utf-8"))
            logging.info(f"Created directory '{self.path_model_dir}' for model files")

    def load_checkpoint(self):
        """ Should load weights and other checkpoint-related data for underlying model of controller. """
        raise NotImplementedError

    def save_checkpoint(self):
        """ Should save weights and other checkpoint-related data for underlying model of controller. """
        pass

    def _train_doc(self, curr_doc, eval_mode=False):
        """ Trains/evaluates (if `eval_mode` is True) model on specific document. Returns predictions, loss and number
            of examples evaluated. """
        raise NotImplementedError

    def train_mode(self):
        """ Set underlying modules to train mode. """
        raise NotImplementedError

    def eval_mode(self):
        """ Set underlying modules to eval mode. """
        raise NotImplementedError

    def train(self, epochs, train_docs, dev_docs):
        logging.info("Starting training")

        best_dev_loss = float("inf")
        t_start = time.time()
        for idx_epoch in range(epochs):
            t_epoch_start = time.time()
            shuffle_indices = torch.randperm(len(train_docs))
            logging.info(f"Running epoch {idx_epoch + 1}/{epochs}")

            self.train_mode()
            train_loss, train_examples = 0.0, 0
            for idx_doc in shuffle_indices:
                curr_doc = train_docs[idx_doc]

                _, (doc_loss, n_examples) = self._train_doc(curr_doc)

                train_loss += doc_loss
                train_examples += n_examples

            self.eval_mode()
            dev_loss, dev_examples = 0.0, 0
            for curr_doc in dev_docs:
                _, (doc_loss, n_examples) = self._train_doc(curr_doc, eval_mode=True)

                dev_loss += doc_loss
                dev_examples += n_examples

            logging.info(f"Training loss: {train_loss / max(1, train_examples): .4f}")
            logging.info(f"Dev loss: {dev_loss / max(1, dev_examples): .4f}")

            if (dev_loss / dev_examples) < best_dev_loss:
                self.save_checkpoint()
                # Save this score as best
                best_dev_loss = dev_loss / dev_examples

            logging.info(f"\tEpoch #{1 + idx_epoch} took {time.time() - t_epoch_start:.2f}s")
            logging.info("")

        logging.info(f"Training complete: took {time.time() - t_start:.2f}s")

        # Add model train scores to model metadata
        with open(self.path_metadata, "a", encoding="utf-8") as f:
            logging.info(f"Saving best validation score to {self.path_metadata}")
            f.writelines([
                "\n",
                "Train model scores:\n",
                f"Best validation set loss: {best_dev_loss}\n",
            ])

    def evaluate(self, test_docs):
        # doc_name: <cluster assignments> pairs for all test documents
        logging.info("Evaluating...")
        all_test_preds = {}

        # [MUC score]
        # The MUC score counts the minimum number of links between mentions
        # to be inserted or deleted when mapping a system response to a gold standard key set
        # [B3 score]
        # B3 computes precision and recall for all mentions in the document,
        # which are then combined to produce the final precision and recall numbers for the entire output
        # [CEAF score]
        # CEAF applies a similarity metric (either mention based or entity based) for each pair of entities
        # (i.e. a set of mentions) to measure the goodness of each possible alignment.
        # The best mapping is used for calculating CEAF precision, recall and F-measure
        muc_score = metrics.Score()
        b3_score = metrics.Score()
        ceaf_score = metrics.Score()

        logging.info("Evaluation with MUC, BCube and CEAF score...")
        for curr_doc in test_docs:

            test_preds, _ = self._train_doc(curr_doc, eval_mode=True)
            test_clusters = get_clusters(test_preds)

            # Save predicted clusters for this document id
            all_test_preds[curr_doc.doc_id] = test_clusters

            # input into metric functions should be formatted as dictionary of {int -> set(str)},
            # where keys (ints) are clusters and values (string sets) are mentions in a cluster. Example:
            # {
            #  1: {'rc_1', 'rc_2', ...}
            #  2: {'rc_5', 'rc_8', ...}
            #  3: ...
            # }

            # gt = ground truth, pr = predicted by model
            gt_clusters = {k: set(v) for k, v in enumerate(curr_doc.clusters)}
            pr_clusters = {}
            for (pr_ment, pr_clst) in test_clusters.items():
                if pr_clst not in pr_clusters:
                    pr_clusters[pr_clst] = set()
                pr_clusters[pr_clst].add(pr_ment)

            muc_score.add(metrics.muc(gt_clusters, pr_clusters))
            b3_score.add(metrics.b_cubed(gt_clusters, pr_clusters))
            ceaf_score.add(metrics.ceaf_e(gt_clusters, pr_clusters))

        logging.info(f"----------------------------------------------")
        logging.info(f"**Test scores**")
        logging.info(f"**MUC:      {muc_score}**")
        logging.info(f"**BCubed:   {b3_score}**")
        logging.info(f"**CEAFe:    {ceaf_score}**")
        logging.info(f"**CoNLL-12: {metrics.conll_12(muc_score, b3_score, ceaf_score)}**")
        logging.info(f"----------------------------------------------")

        # Save test predictions and scores to file for further debugging
        with open(self.path_pred_scores, "w", encoding="utf-8") as f:
            f.writelines([
                f"Database: {self.dataset_name}\n\n",
                f"Test scores:\n",
                f"MUC:      {muc_score}\n",
                f"BCubed:   {b3_score}\n",
                f"CEAFe:    {ceaf_score}\n",
                f"CoNLL-12: {metrics.conll_12(muc_score, b3_score, ceaf_score)}\n",
            ])
        with open(self.path_pred_clusters, "w", encoding="utf-8") as f:
            f.writelines(["Predictions:\n"])
            for doc_id, clusters in all_test_preds.items():
                f.writelines([
                    f"Document '{doc_id}':\n",
                    str(clusters), "\n"
                ])

    def visualize(self):
        build_and_display(self.path_pred_clusters, self.path_pred_scores, self.path_model_dir, display=False)
