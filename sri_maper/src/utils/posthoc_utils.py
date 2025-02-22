import torch
from torch import nn, optim
from itertools import islice
import numpy as np

# from sri_maper.src.models.cma_module import CMALitModule
from sri_maper.src import utils
log = utils.get_pylogger(__name__)


class BinaryTemperatureScaling(nn.Module):
    """
    A class that calculates the optimal temperature scaling for a
    model (nn.Module):
        A classification neural network
        NB: Output of the neural network should be the classification logits,
            NOT the softmax (or log softmax)!
    """
    def __init__(self, model):
        super(BinaryTemperatureScaling, self).__init__()
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = model.to(device)
        self.temperature = torch.tensor(1.5, dtype=torch.float, device=self.model.device, requires_grad=True)

    def temperature_scale(self, logits):
        """
        Perform temperature scaling on logits
        """
        # Expand temperature to match the size of logits
        return logits / self.temperature

    # This function probably should live outside of this class, but whatever
    def calibrate(self, datamodule, val_fraction):
        """
        Tune the tempearature of the model (using the validation set).
        We're going to set it to optimize NLL.
        
        """
        nll_criterion = nn.BCEWithLogitsLoss().to(self.model.device)
        ece_criterion = _ECELoss().to(self.model.device)

        val_loader = datamodule.val_dataloader(shuffle=True)
        batch_limit = int(len(val_loader)*val_fraction)

        # First: collect all the logits and labels for the validation set
        logits_list = []
        labels_list = []
        with torch.no_grad():
            for inputs, label in islice(val_loader, batch_limit):
                inputs = inputs.to(dtype=torch.float32, device=self.model.device)
                logits = self.model(inputs).detach()
                logits_list.append(logits)
                labels_list.append(label)
            logits = torch.cat(logits_list).to(dtype=torch.float, device=self.model.device).reshape(-1,1)
            labels = torch.cat(labels_list).to(dtype=torch.float, device=self.model.device).reshape(-1,1)

        # Calculate NLL and ECE before temperature scaling
        before_temperature_nll = nll_criterion(logits, labels).item()
        before_temperature_ece = ece_criterion(logits, labels).item()
        log.info('Before temperature - NLL: %.3f, ECE: %.3f' % (before_temperature_nll, before_temperature_ece))

        # Next: optimize the temperature w.r.t. NLL
        optimizer = optim.LBFGS([self.temperature], lr=0.01, max_iter=50)

        def eval():
            optimizer.zero_grad()
            loss = nll_criterion(self.temperature_scale(logits), labels)
            loss.backward()
            return loss
        optimizer.step(eval)

        # Calculate NLL and ECE after temperature scaling
        after_temperature_nll = nll_criterion(self.temperature_scale(logits), labels).item()
        after_temperature_ece = ece_criterion(self.temperature_scale(logits), labels).item()
        log.info('After temperature - NLL: %.3f, ECE: %.3f' % (after_temperature_nll, after_temperature_ece))

        return self.temperature.item()


class _ECELoss(nn.Module):
    """
    Calculates the Expected Calibration Error of a model.
    (This isn't necessary for temperature scaling, just a cool metric).

    The input to this loss is the logits of a model, NOT the softmax scores.

    This divides the confidence outputs into equally-sized interval bins.
    In each bin, we compute the confidence gap:

    bin_gap = | avg_confidence_in_bin - accuracy_in_bin |

    We then return a weighted average of the gaps, based on the number
    of samples in each bin

    See: Naeini, Mahdi Pakdaman, Gregory F. Cooper, and Milos Hauskrecht.
    "Obtaining Well Calibrated Probabilities Using Bayesian Binning." AAAI.
    2015.
    """
    def __init__(self, n_bins=15):
        """
        n_bins (int): number of confidence interval bins
        """
        super(_ECELoss, self).__init__()
        bin_boundaries = torch.linspace(0, 1, n_bins + 1)
        self.bin_lowers = bin_boundaries[:-1]
        self.bin_uppers = bin_boundaries[1:]

    def forward(self, logits, labels):
        confidences = torch.sigmoid(logits)
        predictions = confidences > 0.5
        accuracies = predictions.eq(labels)

        ece = torch.zeros(1, device=logits.device)
        for bin_lower, bin_upper in zip(self.bin_lowers, self.bin_uppers):
            # Calculated |confidence - accuracy| in each bin
            in_bin = confidences.gt(bin_lower.item()) * confidences.le(bin_upper.item())
            prop_in_bin = in_bin.float().mean()
            if prop_in_bin.item() > 0:
                accuracy_in_bin = accuracies[in_bin].float().mean()
                avg_confidence_in_bin = confidences[in_bin].mean()
                ece += torch.abs(avg_confidence_in_bin - accuracy_in_bin) * prop_in_bin

        return ece


class ThresholdMoving(nn.Module):
    """
        A class to search for the best classification threshold of the model 
        (using the validation set) w.r.t. a provided metric.
    
    """
    def __init__(self, model):
        super(ThresholdMoving, self).__init__()
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = model.to(device)

    def search_threshold(self, max_metric, datamodule, val_fraction):
        val_loader = datamodule.val_dataloader(shuffle=True)
        batch_limit = int(len(val_loader)*val_fraction)

        # collects all the logits and labels for the validation set
        logits = []
        labels = []
        with torch.no_grad():
            for inputs, label in islice(val_loader, batch_limit):
                inputs = inputs.to(dtype=torch.float32, device=self.model.device)
                logit = torch.sigmoid(self.model.calibrated_forward(inputs))
                logits.append(logit.detach().cpu().numpy())
                labels.append(label.detach().cpu().numpy())
            logits = np.concatenate(logits, axis=None)
            labels = np.concatenate(labels, axis=None)
    
        # search thresholds for imbalanced classification
        thresholds = np.arange(0, 1, 0.001)
        # evaluate each threshold
        scores = [max_metric(labels, self.to_labels(logits, t)) for t in thresholds]
        
        # get best threshold
        ix = np.argmax(scores)
        log.info(f"Threshold={thresholds[ix]:.3f}, Validation F-Score={scores[ix]:.5f}")

        return thresholds[ix]

    @staticmethod
    def to_labels(pos_probs, threshold):
        return (pos_probs >= threshold).astype('int')
