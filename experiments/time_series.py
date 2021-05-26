from statsmodels.tsa.arima_process import arma_generate_sample
from visualization.time_series import plot_time_series
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MinMaxScaler
from models.time_series_forecasting import TimeSeriesForecaster
from explainers.simplex import Simplex
from explainers.nearest_neighbours import NearNeighLatent
from utils.schedulers import ExponentialScheduler
from sklearn.metrics import r2_score
import numpy as np
import torch
import os
import pickle as pkl
import argparse
import matplotlib.pyplot as plt


class TimeSeriesDataset(torch.utils.data.Dataset):
    def __init__(self, X, y):
        super(TimeSeriesDataset).__init__()
        self.X = X
        self.y = y

    def __len__(self):
        return len(self.X)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]


def generate_arma_old(random_seed: int = 42):
    ar_coefs = [1, .99]
    ma_coefs = [.0001, 0]
    np.random.seed(random_seed)
    X = arma_generate_sample(ar_coefs, ma_coefs, nsample=(10000, 31))
    X, Y = X[:, 10:-1], X[:, 11:]
    return X, Y


def generate_ar(ar_coefs: np.ndarray, random_seed: int = 42, length: int = 50, n_samples: int = 10000,
                variance: float = .1):
    np.random.seed(random_seed)
    p = len(ar_coefs)
    X = np.zeros((n_samples, length+1))
    X[:, :p] = variance*np.random.randn(n_samples, p)
    Noise = variance*np.random.randn(n_samples, length+1)

    # Better initialization
    for t in range(3*p):
        X_p = X[:, :p] @ ar_coefs[::-1] + variance*np.random.randn(n_samples)
        X[:, :p-1] = X[:, 1:p]
        X[:, p-1] = X_p

    for t in range(p, length+1):
        X[:, t] = X[:, t-p:t] @ ar_coefs[::-1] + Noise[:, t]

    return X[:, :-1], X[:, 1:]


def ar_precision(random_seed: int = 42, cv: int = 0, save_path: str='./results/ar/precision', train=True):

    print(100 * '-' + '\n' + 'Welcome in the approximation quality experiment for AR. \n'
                             f'Settings: random_seed = {random_seed} ; cv = {cv}.\n'
          + 100 * '-')

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    torch.manual_seed(random_seed)
    explainers_name = ['simplex', 'nn_uniform', 'nn_dist', 'representer']

    # Create saving directory if inexistent
    if not os.path.exists(save_path):
        print(f'Creating the saving directory {save_path}')
        os.makedirs(save_path)

    ar_coefs = np.array([.7, .25])
    length = 50
    n_samples = 10000
    corpus_size = 1000
    batch_size_simplex = 100
    n_epoch_simplex = 20000
    k_list = [k for k in range(2,10)] + [k for k in range(10, 51, 5)]

    X, Y = generate_ar(ar_coefs, random_seed + cv, length, n_samples)
    X_train, X_test, Y_train, Y_test = train_test_split(X, Y, test_size=0.1, random_state=random_seed + cv)
    X_train = X_train.reshape(len(X_train), -1, 1)
    X_train = torch.from_numpy(X_train).float()
    X_test = X_test.reshape(len(X_test), -1, 1)
    X_test = torch.from_numpy(X_test).float().to(device)
    Y_train = Y_train.reshape(len(Y_train), -1, 1)
    Y_train = torch.from_numpy(Y_train).float()
    Y_test = Y_test.reshape(len(Y_test), -1, 1)
    Y_test = torch.from_numpy(Y_test).float().to(device)
    training_set = TimeSeriesDataset(X_train, Y_train)
    test_set = TimeSeriesDataset(X_test, Y_test)
    train_loader = torch.utils.data.DataLoader(training_set, batch_size=20, shuffle=True)

    model = TimeSeriesForecaster().to(device)
    opt = torch.optim.Adam(params=model.parameters())

    '''
    model.hidden = model.init_hidden(batch_size=1)
    plt.plot(np.arange(length), Y_test[2].detach().cpu().numpy())
    plt.plot(np.arange(length), model(X_test[2:3])[0].detach().cpu().numpy())
    plt.show()
    '''
    if train:
        model.hidden = model.init_hidden(batch_size=len(X_test))
        print(f'Initial Test MSE: {torch.mean((Y_test - model(X_test)) ** 2):.3g}')
        model.train()
        for epoch in range(20):
            for X, Y in train_loader:
                X = X.to(device)
                Y = Y.to(device)
                model.hidden = model.init_hidden(batch_size=len(X))
                opt.zero_grad()
                Y_pred = model(X)
                error = torch.sum((Y - Y_pred) ** 2)
                error.backward()
                opt.step()
            if (epoch+1) % 5 == 0:
                model.hidden = model.init_hidden(batch_size=len(X_test))
                print(f'Epoch {epoch + 1}: Test MSE = {torch.mean((Y_test - model(X_test)) ** 2):.3g}.')
        model_path = os.path.join(save_path, f'model_cv{cv}.pth')
        print(f'Saving the model in {model_path}.')
        torch.save(model.state_dict(), model_path)

    model = TimeSeriesForecaster()
    model.load_state_dict(torch.load(os.path.join(save_path, f'model_cv{cv}.pth')))
    model.to(device)

    '''
    model.hidden = model.init_hidden(batch_size=1)
    plt.plot(np.arange(length), Y_test[2].detach().cpu().numpy())
    plt.plot(np.arange(length), model(X_test[2:3])[0].detach().cpu().numpy())
    plt.show()
    '''
    corpus_loader = torch.utils.data.DataLoader(training_set, batch_size=corpus_size, shuffle=True)
    test_loader = torch.utils.data.DataLoader(test_set, batch_size=batch_size_simplex)
    X_corpus, _ = next(iter(corpus_loader))
    X_corpus = X_corpus.to(device)
    model.hidden = model.init_hidden(len(X_corpus))
    latent_corpus = model.latent_representation(X_corpus).detach()
    reg_scheduler = ExponentialScheduler(1.0e-5, 1.0e-3, n_epoch_simplex)

    simplex = Simplex(X_corpus, latent_corpus)
    knn_uniform = NearNeighLatent(X_corpus, latent_corpus)
    knn_dist = NearNeighLatent(X_corpus, latent_corpus, weights_type='distance')

    for k in k_list:
        print(20*'-'+f'Now working with {k} active corpus members' + 20*'-')
        latent_true = np.zeros((len(X_test), model.hidden_dim))
        latent_simplex = np.zeros((len(X_test), model.hidden_dim))
        latent_knn_uniform = np.zeros((len(X_test), model.hidden_dim))
        latent_knn_dist = np.zeros((len(X_test), model.hidden_dim))
        output_true = np.zeros((len(X_test), model.output_dim))
        output_simplex = np.zeros((len(X_test), model.output_dim))
        output_knn_uniform = np.zeros((len(X_test), model.output_dim))
        output_knn_dist = np.zeros((len(X_test), model.output_dim))
        for n_batch, (x_test, _) in enumerate(test_loader):
            print(20 * '-' + f'Now working with batch {n_batch+1} / {int(len(X_test)/batch_size_simplex)}' + 20 * '-')
            x_test = x_test.to(device)
            model.hidden = model.init_hidden(len(x_test))
            latent_test = model.latent_representation(x_test).detach()
            latent_true[n_batch*batch_size_simplex:(n_batch+1)*batch_size_simplex, :] =\
                latent_test.cpu().numpy()
            output_true[n_batch * batch_size_simplex:(n_batch + 1) * batch_size_simplex, :] = \
                model.latent_to_output(latent_test).detach().cpu().numpy()
            simplex.fit(X_test, latent_test, n_epoch=n_epoch_simplex, reg_factor=1.0e-5,
                        reg_factor_scheduler=reg_scheduler, n_keep=k)
            latent_simplex[n_batch*batch_size_simplex:(n_batch+1)*batch_size_simplex, :] =\
                simplex.latent_approx().cpu().numpy()
            output_simplex[n_batch * batch_size_simplex:(n_batch + 1) * batch_size_simplex, :] = \
                model.latent_to_output(simplex.latent_approx()).detach().cpu().numpy()
            knn_uniform.fit(x_test, latent_test, n_keep=k)
            latent_knn_uniform[n_batch*batch_size_simplex:(n_batch+1)*batch_size_simplex, :] =\
                knn_uniform.latent_approx().cpu().numpy()
            output_knn_uniform[n_batch * batch_size_simplex:(n_batch + 1) * batch_size_simplex, :] = \
                model.latent_to_output(knn_uniform.latent_approx()).detach().cpu().numpy()
            knn_dist.fit(x_test, latent_test, n_keep=k)
            latent_knn_dist[n_batch * batch_size_simplex:(n_batch + 1) * batch_size_simplex, :] = \
                knn_dist.latent_approx().cpu().numpy()
            output_knn_dist[n_batch * batch_size_simplex:(n_batch + 1) * batch_size_simplex, :] = \
                model.latent_to_output(knn_dist.latent_approx()).detach().cpu().numpy()

        print(f'SimplEx: R2 Latent = {r2_score(latent_true, latent_simplex):.3g} ; '
              f'R2 Output = {r2_score(output_true, output_simplex):.3g}')
        print(f'KNN Uniform: R2 Latent = {r2_score(latent_true, latent_knn_uniform):.3g} ; '
              f'R2 Output = {r2_score(output_true, output_knn_uniform):.3g}')
        print(f'KNN Distance: R2 Latent = {r2_score(latent_true, latent_knn_dist):.3g} ; '
              f'R2 Output = {r2_score(output_true, output_knn_dist):.3g}')
        print(f'Saving the results in {save_path}.')
        with open(os.path.join(save_path, f'true_k{k}_cv{cv}'), 'wb') as f:
            pkl.dump((latent_true, output_true), f)
        with open(os.path.join(save_path, f'simplex_k{k}_cv{cv}'), 'wb') as f:
            pkl.dump((latent_simplex, output_simplex), f)
        with open(os.path.join(save_path, f'knn_uniform_k{k}_cv{cv}'), 'wb') as f:
            pkl.dump((latent_knn_uniform, output_knn_uniform), f)
        with open(os.path.join(save_path, f'knn_dist_k{k}_cv{cv}'), 'wb') as f:
            pkl.dump((latent_knn_dist, output_knn_dist), f)




def main(experiment: str = 'precision', cv: int = 0):
    if experiment == 'precision':
        ar_precision(cv=cv)
    elif experiment == 'outlier_detection':
        pass


parser = argparse.ArgumentParser()
parser.add_argument('-experiment', type=str, default='precision', help='Experiment to perform')
parser.add_argument('-cv', type=int, default=0, help='Cross validation parameter')
args = parser.parse_args()

if __name__ == '__main__':
    main(args.experiment, args.cv)
