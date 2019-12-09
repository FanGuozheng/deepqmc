import pytest
import torch
from torch import nn

from deepqmc.fit import LossWeightedLogProb, fit_wfnet, simple_sampler
from deepqmc.geom import get_system
from deepqmc.nn import GTOBasis, PauliNet
from deepqmc.nn.schnet import ElectronicSchnet
from deepqmc.physics import local_energy
from deepqmc.sampling import LangevinSampler

try:
    import pyscf.gto
except ImportError:
    pyscf_marks = [pytest.mark.skip(reason='Pyscf not installed')]
else:
    pyscf_marks = []


def assert_alltrue_named(items):
    dct = dict(items)
    assert dct == {k: True for k in dct}


@pytest.fixture
def rs():
    return torch.randn(5, 3, 3)


@pytest.fixture
def geom():
    return get_system('H2')['geom']


@pytest.fixture(params=[pytest.param(PauliNet, marks=pyscf_marks)])
def net_factory(request):
    return request.param


class JastrowNet(nn.Module):
    def __init__(self, n_atoms, n_features, n_up, n_down):
        super().__init__()
        self.schnet = ElectronicSchnet(
            n_up, n_down, n_atoms, 2, basis_dim=4, kernel_dim=8, embedding_dim=16
        )
        self.orbital = nn.Linear(16, 1)

    def forward(self, xs, **kwargs):
        xs = self.schnet(xs)
        return self.orbital(xs).squeeze(dim=-1).sum(dim=-1)


@pytest.fixture
def wfnet(net_factory, geom):
    args = (geom, 3, 0)
    kwargs = {}
    if net_factory is PauliNet:
        mol = pyscf.gto.M(atom=geom.as_pyscf(), unit='bohr', basis='6-311g', cart=True)
        basis = GTOBasis.from_pyscf(mol)
        args += (basis,)
        kwargs.update(
            {
                'cusp_correction': True,
                'cusp_electrons': True,
                'jastrow_factory': JastrowNet,
                'dist_basis_dim': 4,
            }
        )
    return net_factory(*args, **kwargs)


def test_batching(wfnet, rs):
    assert torch.allclose(wfnet(rs[:2]), wfnet(rs)[:2], atol=0)


def test_antisymmetry(wfnet, rs):
    assert torch.allclose(wfnet(rs[:, [0, 2, 1]]), -wfnet(rs), atol=0)


def test_antisymmetry_trained(wfnet, rs):
    sampler = LangevinSampler(wfnet, torch.rand_like(rs), tau=0.1)
    fit_wfnet(
        wfnet,
        LossWeightedLogProb(),
        torch.optim.Adam(wfnet.parameters(), lr=1e-2),
        simple_sampler(sampler),
        range(10),
    )
    assert torch.allclose(wfnet(rs[:, [0, 2, 1]]), -wfnet(rs), atol=0)


def test_backprop(wfnet, rs):
    wfnet(rs).sum().backward()
    assert_alltrue_named(
        (name, param.grad is not None) for name, param in wfnet.named_parameters()
    )
    assert_alltrue_named(
        (name, param.grad.sum().abs().item() > 0)
        for name, param in wfnet.named_parameters()
    )


def test_grad(wfnet, rs):
    rs.requires_grad_()
    wfnet(rs).sum().backward()
    assert rs.grad.sum().abs().item() > 0


def test_loc_ene_backprop(wfnet, rs):
    rs.requires_grad_()
    Es_loc, _ = local_energy(rs, wfnet, create_graph=True)
    Es_loc.sum().backward()
    assert_alltrue_named(
        (name, param.grad.sum().abs().item() > 0)
        for name, param in wfnet.named_parameters()
    )
