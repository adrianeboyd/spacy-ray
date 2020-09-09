"""
PyTorch version: https://github.com/pytorch/examples/blob/master/mnist/main.py
TensorFlow version: https://github.com/tensorflow/tensorflow/blob/master/tensorflow/examples/tutorials/mnist/mnist.py
"""
# pip install thinc ml_datasets typer
import time
from thinc.api import Model, chain, Relu, Softmax, Adam
import ml_datasets
from wasabi import msg
from tqdm import tqdm
import typer
from spacy_ray.thinc_proxies import RayProxy
from spacy_ray.thinc_shared_optimizer import SharedOptimizer
from spacy_ray.util import set_params_proxy
import ray

class Timer:
    def __init__(self, state):
        self.state = state
        self.sum = 0
        self.n = 0

    def __enter__(self):
        self.start = time.time()
        self.n += 1

    def __exit__(self, *args):
        interval = time.time() - self.start
        self.sum += interval
        print(f"{self.state}: {self.sum / self.n:0.4f}")

@ray.remote
class Worker:
    def __init__(self, i, n_workers):
        self.i = i
        self.n_workers = n_workers
        self.model = None
        self.optimizer = None
        self.train_data = None
        self.dev_data = None
        self.timers = {k: Timer(k) for k in ["forward", "backprop"]}
        self.conn = None

    def add_model(self, n_hidden, dropout):
        # Define the model
        self.model = chain(
            Relu(nO=n_hidden, dropout=dropout),
            Relu(nO=n_hidden, dropout=dropout),
            Softmax(),
        )

    def add_data(self, batch_size):
        # Load the data
        (train_X, train_Y), (dev_X, dev_Y) = ml_datasets.mnist()
        shard_size = len(train_X) // self.n_workers
        shard_start = self.i * shard_size
        shard_end = shard_start + shard_size
        self.train_data = self.model.ops.multibatch(
            batch_size,
            train_X[shard_start : shard_end],
            train_Y[shard_start : shard_end],
            shuffle=True
        )
        self.dev_data = self.model.ops.multibatch(batch_size, dev_X, dev_Y)
        # Set any missing shapes for the model.
        self.model.initialize(X=train_X[:5], Y=train_Y[:5])

    def set_proxy(self, connection, use_thread=False):
        set_params_proxy(self.model, RayProxy(connection, use_thread=use_thread))
        self.conn = connection

    def train_epoch(self):
        for X, Y in self.train_data:
            #with self.timers["forward"]:
            Yh, backprop = self.model.begin_update(X)
            #with self.timers["backprop"]:
            backprop(Yh - Y)
            #with self.timers["finish"]:
            #    self.model.finish_update(self.optimizer)

    def evaluate(self):
        correct = 0
        total = 0
        for X, Y in self.dev_data:
            Yh = self.model.predict(X)
            correct += (Yh.argmax(axis=1) == Y.argmax(axis=1)).sum()
            total += Yh.shape[0]
        return correct / total


def main(
    n_hidden: int = 256, dropout: float = 0.2, n_iter: int = 10, batch_size: int = 256,
    n_epoch: int=10, quorum: int=None, n_workers: int=2, use_thread: bool = False
):
    if quorum is None:
        quorum = n_workers
    batch_size //= n_workers
    ray.init()
    optimizer_config = {
        "optimizer": {
            "@optimizers": "Adam.v1",
            "learn_rate": 0.001
        }
    }
    workers = []
    RemoteOptimizer = ray.remote(SharedOptimizer)
    conn = RemoteOptimizer.remote(optimizer_config, quorum)
    for i in range(n_workers):
        worker = Worker.remote(i, n_workers)
        ray.get(worker.add_model.remote(n_hidden, dropout))
        ray.get(worker.add_data.remote(batch_size))
        ray.get(worker.set_proxy.remote(conn, use_thread))
        workers.append(worker)

    for i in range(n_epoch):
        futures = []
        for worker in workers:
            futures.append(worker.train_epoch.remote())

    with Timer("epoch"):
        ray.get(futures)
    print(i, ray.get(workers[0].evaluate.remote()))


if __name__ == "__main__":
    typer.run(main)
